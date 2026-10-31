import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from typing import ClassVar

import sentry_sdk
from fastapi import FastAPI, Request
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

from app.ai.settings import seed_ai_settings_at_startup
from app.api.auth import router as auth_router
from app.api.books import router as books_router
from app.api.scan import MAX_SCAN_REQUEST_BYTES, SCAN_UPLOAD_PATH
from app.api.scan import router as scan_router
from app.api.user_books import router as user_books_router
from app.core import catalog_token
from app.core.config import check_turnstile_configured, settings
from app.core.database import AsyncSessionLocal
from app.core.limiter import limiter
from app.core.logging import configure_logging, new_request_id, request_id_var
from app.core.origin_auth import OriginAuthMiddleware
from app.core.sentry_context import SentryContextMiddleware, sentry_privacy_options
from app.services.token_cleanup import cleanup_expired_tokens

configure_logging()

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.sentry_release,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=0.2 if settings.is_hardened else 1.0,
        send_default_pii=False,
        **sentry_privacy_options(),
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
        # causes browsers to cache an HSTS policy for localhost, breaking local
        # dev. Sent whenever hardened (staging + production), both of which are
        # public HTTPS deployments.
        if settings.is_hardened:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class InvalidContentLength(ValueError):
    """The request's Content-Length header cannot be trusted."""


def _declared_content_length(headers) -> int | None:
    """The request's declared body length, or None when it declares none.

    Raises InvalidContentLength for a value that is not a plain non-negative
    integer (``abc``, ``-1``, ``+5``, empty) or for several values that
    disagree (repeated headers or a comma-separated list). Identical repeats
    are accepted as that one value (RFC 9110 section 8.6).
    """
    values: set[int] = set()
    for name, raw in headers:
        if name.lower() != b"content-length":
            continue
        for part in raw.split(b","):
            part = part.strip()
            if not part or not part.isdigit():
                raise InvalidContentLength
            values.add(int(part))
    if len(values) > 1:
        raise InvalidContentLength
    return values.pop() if values else None


class RequestSizeLimitMiddleware:
    """Cap every HTTP request body by path, counting the bytes as they arrive.

    Pure ASGI (not BaseHTTPMiddleware) so it sees each ``http.request``
    message and works whether or not the request declares a Content-Length:
    a ``Transfer-Encoding: chunked`` body has none (#557).

    - The scan upload route is capped at MAX_SCAN_REQUEST_BYTES (the 5 MB
      image limit plus multipart overhead); every other route at
      MAX_BODY_SIZE (10 MB).
    - A declared Content-Length over the cap is a 413 at once, without
      reading the body. A malformed one (not a non-negative integer, or
      conflicting values) is a 400.
    - Otherwise the body is counted as the app reads it. Once it passes the
      cap the app is given ``http.disconnect`` instead of more bytes (so
      framework body parsing fails and no handler runs on a truncated body),
      anything the app then tries to send is dropped, and this middleware
      sends the 413 itself, provided no response had started yet.
    - Non-HTTP scopes (lifespan, websocket) pass straight through.
    """

    MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB
    PATH_LIMITS: ClassVar[dict[str, int]] = {SCAN_UPLOAD_PATH: MAX_SCAN_REQUEST_BYTES}

    _TOO_LARGE = (413, b'{"detail": "Request body too large"}')
    _BAD_LENGTH = (400, b'{"detail": "Invalid Content-Length header"}')

    def __init__(self, app) -> None:
        self.app = app

    @classmethod
    def limit_for(cls, path: str) -> int:
        return cls.PATH_LIMITS.get(path.rstrip("/") or "/", cls.MAX_BODY_SIZE)

    @staticmethod
    async def _reply(send, status_body: tuple[int, bytes]) -> None:
        status_code, body = status_body
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # The rest of the body is never read, so the connection
                    # cannot be reused.
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.limit_for(scope.get("path", ""))
        try:
            declared = _declared_content_length(scope.get("headers") or [])
        except InvalidContentLength:
            await self._reply(send, self._BAD_LENGTH)
            return
        if declared is not None and declared > limit:
            await self._reply(send, self._TOO_LARGE)
            return

        received = 0
        exceeded = False
        response_started = False

        async def limited_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message) -> None:
            nonlocal response_started
            if exceeded and not response_started:
                # The app's reaction to the cut-off body (typically a 400
                # "error parsing the body") is replaced by our 413 below.
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            # Whatever the app raised after its body was cut off is a
            # consequence of the cut-off, not a server error.
            if not exceeded:
                raise
        if exceeded and not response_started:
            await self._reply(send, self._TOO_LARGE)


async def _validate_config() -> None:
    """Fail fast on misconfigured deployments before any request is served."""
    check_turnstile_configured(settings)
    # The catalog-token key (#602) is derived from JWT_PRIVATE_KEY; a
    # hardened environment without it must not start.
    catalog_token.check_configured(settings)
    for message in settings.deprecated_settings_warnings:
        logger.warning(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _validate_config()
    # Insert any missing ai_settings rows from the env. Never raises: with the
    # DB down the app still starts and scans fail closed until it is back.
    await seed_ai_settings_at_startup()
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
    docs_url="/docs" if not settings.is_hardened else None,
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
#
# Execution order (outermost → innermost):
#   TrustedHostMiddleware → RequestSizeLimitMiddleware → CORSMiddleware
#   → RequestIdMiddleware → SentryContextMiddleware → SecurityHeadersMiddleware
#   → OriginAuthMiddleware
#
# OriginAuthMiddleware is innermost so its 403 passes back through all outer
# layers: SecurityHeadersMiddleware adds CSP/X-Frame-Options, CORSMiddleware
# adds access-control headers so browsers can read the error body.
# Known limitation: CORSMiddleware short-circuits OPTIONS preflights before
# OriginAuthMiddleware runs, allowing endpoint enumeration via preflight from
# a CORS-allowed origin without the origin-auth header. Actual GET/POST/etc.
# are always checked; no data is exposed via the preflight path.
# The client IP used for rate limiting is not set by a middleware: the
# limiter's key function calls app.core.client_ip directly.
app.add_middleware(OriginAuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SentryContextMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    # Lets web clients read the scan 429s' Retry-After (daily_scan_limit,
    # scan_in_progress, enhanced_throttled) instead of falling back to
    # approximations.
    expose_headers=["Retry-After"],
)
# RequestSizeLimitMiddleware runs before CORS/headers to drop oversized bodies
# early. It is pure ASGI and counts the body bytes as they arrive, so it caps
# chunked uploads (no Content-Length) too: 6 MB on /scan, 10 MB elsewhere (#557).
app.add_middleware(RequestSizeLimitMiddleware)


# TrustedHostMiddleware is outermost when hardened (staging + production) —
# drops requests with spoofed Host headers before any other processing. Only
# /health is exempt, because that liveness probe (CI, uptime monitors, load
# balancers) legitimately hits the origin directly without going through the
# public hostname, and it does nothing an attacker could abuse.
#
# /health/db is deliberately NOT exempt: it opens a DB connection, so it gets
# the same Host (and origin-header) enforcement as every other route. The
# DB-connectivity uptime monitor uses the public hostname, so it doesn't need
# the exemption anyway.
class _HealthExemptTrustedHost(TrustedHostMiddleware):
    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/health":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


if settings.is_hardened:
    app.add_middleware(_HealthExemptTrustedHost, allowed_hosts=settings.trusted_hosts)

app.include_router(auth_router)
app.include_router(books_router)
app.include_router(scan_router)
app.include_router(user_books_router)


@app.get("/health")
@limiter.limit(settings.rate_limit_health)
async def health(request: Request) -> JSONResponse:
    """Liveness probe. Never touches the DB.

    Render restarts a service when its health check fails, so this endpoint
    must stay up (and return 200) even if the database is briefly unreachable
    — otherwise a transient DB blip triggers an unnecessary restart. Use
    ``/health/db`` to check DB connectivity specifically.
    """
    return JSONResponse(status_code=200, content={"status": "ok"})


# Bounded so a hung/slow DB connection can't tie up this endpoint's own worker
# (and its share of the pool) indefinitely.
_HEALTH_DB_TIMEOUT_SECONDS = 5


@app.get("/health/db")
@limiter.limit(settings.rate_limit_health)
async def health_db(request: Request) -> JSONResponse:
    """DB connectivity check, separate from the liveness probe above."""
    try:
        async with AsyncSessionLocal() as session:
            await asyncio.wait_for(
                session.execute(text("SELECT 1")),
                timeout=_HEALTH_DB_TIMEOUT_SECONDS,
            )
        db_status = "ok"
    # Any DB failure (including a timeout) means the health check reports "error".
    except Exception:  # noqa: BLE001 - any DB failure = error
        db_status = "error"

    healthy = db_status == "ok"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "db": db_status},
    )


if not settings.is_hardened:

    @app.get("/debug/sentry-test")
    @limiter.limit("5/minute")
    async def sentry_test(request: Request) -> JSONResponse:
        1 / 0  # noqa: B018 - intentional, triggers Sentry capture

    @app.post("/auth/test-login")
    @limiter.limit("2/minute")
    async def test_login(request: Request) -> JSONResponse:
        """Dev-only login for E2E tests. Requires TEST_AUTH_SECRET.

        This endpoint is NOT registered when hardened — staging or production
        (guarded by the ``if not settings.is_hardened`` block), and a hardened
        process with TEST_AUTH_SECRET set refuses to boot. Even in dev
        it requires a shared secret and only issues tokens for emails
        already in the ALLOWED_EMAILS allowlist.
        """
        from app.auth.identity import (
            ACCOUNT_CONFLICT,
            AccountConflictError,
            email_allowed,
            resolve_and_commit_google_user,
        )
        from app.auth.sessions import issue_session_tokens
        from app.core.database import get_db

        # A whitespace-only secret counts as unset: the route stays disabled.
        if not settings.test_auth_secret.strip():
            return JSONResponse(
                status_code=403,
                content={"detail": "TEST_AUTH_SECRET not configured"},
            )

        try:
            body = await request.json()
        except ValueError:  # not JSON (json.JSONDecodeError / bad encoding)
            return JSONResponse(status_code=422, content={"detail": "Invalid body"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=422, content={"detail": "Invalid body"})
        provided_secret = body.get("secret", "")
        email = body.get("email", "")
        if not isinstance(provided_secret, str) or not isinstance(email, str):
            return JSONResponse(status_code=422, content={"detail": "Invalid body"})

        if not hmac.compare_digest(
            provided_secret.encode("utf-8", "surrogatepass"),
            settings.test_auth_secret.encode("utf-8", "surrogatepass"),
        ):
            return JSONResponse(status_code=403, content={"detail": "Invalid secret"})

        if not email_allowed(email, settings.allowed_emails_list):
            return JSONResponse(
                status_code=403, content={"detail": "Email not in allowlist"}
            )

        async for db in get_db():
            # Same identity rule as /auth/google (#688): matched by a
            # synthetic subject (test_<lowercased email>) that no real Google
            # ``sub`` (all digits) can take, never by email. An email held by
            # a real account is a 409.
            try:
                user = await resolve_and_commit_google_user(
                    db,
                    google_id=f"test_{email.lower()}",
                    email=email,
                    display_name="E2E Test User",
                    avatar_url=None,
                )
            except AccountConflictError:
                return JSONResponse(
                    status_code=409, content={"detail": ACCOUNT_CONFLICT}
                )

            tokens = await issue_session_tokens(db, user)

            logger.info("test-login: issued tokens (user_id=%s)", user.id)
            return JSONResponse(
                content={
                    "access_token": tokens.access_token,
                    "refresh_token": tokens.refresh_token,
                    "expires_in": tokens.expires_in,
                }
            )
