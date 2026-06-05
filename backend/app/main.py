import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import sentry_sdk
from fastapi import FastAPI, Request
from sqlalchemy import select
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi import _rate_limit_exceeded_handler as _slowapi_429
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app.api.auth import router as auth_router
from app.api.books import router as books_router
from app.api.scan import router as scan_router
from app.api.user_books import router as user_books_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.limiter import limiter
from app.core.logging import configure_logging, new_request_id, request_id_var
from app.core.sentry_context import SentryContextMiddleware
from app.services.token_cleanup import cleanup_expired_tokens

configure_logging()

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=0.2 if settings.environment == "production" else 1.0,
        send_default_pii=False,
    )

logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        rid = new_request_id()
        request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # Setting to "0" is the current OWASP recommendation; "1;mode=block" can
        # introduce reflected XSS in older browsers and is not needed when CSP is set.
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )
        # Pure JSON API: deny all document rendering and framing.
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        # Prevents cross-origin sites from embedding this API's responses as a
        # resource (e.g. via <img>, <script>). Safe for a JSON-only API.
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # HSTS must only be sent over HTTPS. Sending it over HTTP in development
        # causes browsers to cache an HSTS policy for localhost, breaking local dev.
        if settings.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class CloudflareRealIPMiddleware(BaseHTTPMiddleware):
    """Restore the real client IP from the CF-Connecting-IP header set by Cloudflare.

    Without this, slowapi keys unauthenticated rate limits by Cloudflare's edge IP
    rather than the real client IP, effectively disabling per-IP rate limiting.
    The production guard prevents a locally supplied CF-Connecting-IP header from
    being trusted in dev/staging environments.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip and settings.environment == "production":
            request.scope["client"] = (cf_ip, 0)
        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the limit before reading the body.

    Prevents memory exhaustion from large request bodies that bypass per-endpoint
    size checks (e.g. chunked transfer encoding without Content-Length).
    """

    MAX_BODY_SIZE = (
        10 * 1024 * 1024
    )  # 10 MB — covers the 5 MB image limit with headroom

    async def dispatch(self, request: Request, call_next) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.MAX_BODY_SIZE:
            return Response(
                content='{"detail": "Request body too large"}',
                status_code=413,
                media_type="application/json",
            )
        return await call_next(request)


async def _validate_config() -> None:
    """Fail fast on misconfigured deployments before any request is served."""
    if settings.turnstile_required and not settings.turnstile_secret_key:
        raise RuntimeError(
            "TURNSTILE_SECRET_KEY must be configured when TURNSTILE_REQUIRED=true. "
            "Set TURNSTILE_REQUIRED=false to explicitly disable bot protection "
            "(dev / test environments only)."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _validate_config()
    cleanup_task = asyncio.create_task(cleanup_expired_tokens())
    yield
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="Bookshelf API",
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    logger.warning("rate_limit_exceeded path=%s", request.url.path)
    return _slowapi_429(request, exc)


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Middleware stack — add_middleware wraps in reverse order so the last call added
# becomes the outermost layer (first to run on incoming requests).
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SentryContextMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(CloudflareRealIPMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
)
# RequestSizeLimitMiddleware runs before CORS/headers to drop oversized bodies early.
app.add_middleware(RequestSizeLimitMiddleware)


# TrustedHostMiddleware is outermost in production — drops requests with spoofed
# Host headers before any other processing. /health is exempt because health
# probes (CI, uptime monitors, load balancers) legitimately hit the origin
# directly without going through the public hostname.
class _HealthExemptTrustedHost(TrustedHostMiddleware):
    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


if settings.environment == "production":
    app.add_middleware(_HealthExemptTrustedHost, allowed_hosts=settings.trusted_hosts)

app.include_router(auth_router)
app.include_router(books_router)
app.include_router(scan_router)
app.include_router(user_books_router)


@app.get("/health")
@limiter.limit(settings.rate_limit_health)
async def health(request: Request) -> JSONResponse:
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "error"

    healthy = db_status == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "db": db_status},
    )


if settings.environment != "production":

    @app.get("/debug/sentry-test")
    @limiter.limit("5/minute")
    async def sentry_test(request: Request) -> JSONResponse:
        1 / 0  # intentional — triggers Sentry capture

    @app.post("/auth/test-login")
    @limiter.limit("2/minute")
    async def test_login(request: Request) -> JSONResponse:
        """Dev-only login for E2E tests. Requires TEST_AUTH_SECRET.

        This endpoint is NOT registered in production (guarded by the
        ``if settings.environment != "production"`` block).  Even in dev
        it requires a shared secret and only issues tokens for emails
        already in the ALLOWED_EMAILS allowlist.
        """
        from app.auth.jwt import create_access_token, create_refresh_token
        from app.core.database import get_db
        from app.models.refresh_token import RefreshToken as RefreshTokenModel
        from app.models.user import User

        if not settings.test_auth_secret:
            return JSONResponse(
                status_code=403,
                content={"detail": "TEST_AUTH_SECRET not configured"},
            )

        body = await request.json()
        provided_secret = body.get("secret", "")
        email = body.get("email", "")

        if not hmac.compare_digest(provided_secret, settings.test_auth_secret):
            return JSONResponse(status_code=403, content={"detail": "Invalid secret"})

        if settings.allowed_emails_list and email not in settings.allowed_emails_list:
            return JSONResponse(
                status_code=403, content={"detail": "Email not in allowlist"}
            )

        async for db in get_db():
            result = await db.execute(select(User).where(User.email == email))
            user = result.scalar_one_or_none()
            if user is None:
                user = User(
                    email=email,
                    google_id=f"test_{email}",
                    display_name="E2E Test User",
                )
                db.add(user)
                await db.commit()
                await db.refresh(user)

            access_token = create_access_token(str(user.id), user.email)
            refresh_token, jti = create_refresh_token(str(user.id))
            expires_at = datetime.now(timezone.utc) + timedelta(
                days=settings.refresh_token_expiry_days
            )
            db.add(RefreshTokenModel(jti=jti, user_id=user.id, expires_at=expires_at))
            await db.commit()

            logger.info("test-login: issued tokens for %s", email)
            return JSONResponse(
                content={
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_in": settings.jwt_expiry_hours * 3600,
                }
            )
