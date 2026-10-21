import logging
from datetime import date

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.file_security import sanitize_image, scan_for_malware
from app.core.file_validation import validate_magic_bytes
from app.core.limiter import limiter
from app.models.user import User
from app.schemas.book import EnrichedBook, ScanResponse
from app.services import scan_quota
from app.services.book_identifier import ScanUnavailableError
from app.services.chatgpt_vision import ChatGPTVisionIdentifier
from app.services.deduplication import DeduplicationService
from app.services.enrichment import EnrichmentService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def _verify_turnstile(token: str) -> bool:
    """Verify a Cloudflare Turnstile token against the siteverify endpoint.

    Returns True if the token is valid, False otherwise.
    A 5-second timeout prevents a Turnstile outage from blocking the endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": settings.turnstile_secret_key, "response": token},
            )
            resp.raise_for_status()
            return bool(resp.json().get("success"))
    # Any Turnstile failure (network, timeout, malformed response) is treated as
    # verification failure rather than propagating.
    except Exception as exc:  # noqa: BLE001
        logger.warning("Turnstile verification failed: %s", exc)
        return False


def _dedupe_enriched(books: list[EnrichedBook]) -> list[EnrichedBook]:
    """Drop books that resolved to the same work as an earlier one.

    The vision model dedupes by title/author, but two differently-read spines
    (e.g. a subtitle vs. none) can still enrich to the same Open Library work.
    """
    seen: set[str] = set()
    unique: list[EnrichedBook] = []
    for book in books:
        keys = [
            f"ol:{book.open_library_work_id}" if book.open_library_work_id else None,
            f"gb:{book.google_books_id}" if book.google_books_id else None,
        ]
        keys = [k for k in keys if k]
        if any(k in seen for k in keys):
            continue
        seen.update(keys)
        unique.append(book)
    return unique


async def _spend_enhanced_credit(db: AsyncSession, user: User) -> int:
    """Atomically decrement the user's enhanced-scan credits; return the balance.

    The ``> 0`` guard makes two concurrent enhanced scans unable to drive the
    balance negative — the loser simply isn't charged.
    """
    result = await db.execute(
        update(User)
        .where(User.id == user.id, User.enhanced_scan_credits > 0)
        .values(enhanced_scan_credits=User.enhanced_scan_credits - 1)
        .returning(User.enhanced_scan_credits)
    )
    remaining = result.scalar_one_or_none()
    await db.commit()
    return int(remaining) if remaining is not None else 0


async def _reserve_daily_scan(user: User, usage_day: date) -> None:
    """Enforce the global and per-user daily caps, reserving one scan.

    Uses its own short-lived session so the reservation commits on its own,
    independent of the request's session.
    """
    async with AsyncSessionLocal() as quota_db:
        used_globally = await scan_quota.global_scans_today(quota_db, usage_day)
        if used_globally >= settings.scan_global_daily_limit:
            logger.warning("scan: global daily limit reached (%d)", used_globally)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="scan_capacity",
            )
        reserved = await scan_quota.reserve_scan(
            quota_db, user.id, usage_day, settings.scan_daily_limit_per_user
        )
    if reserved is None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily_scan_limit",
            headers={"Retry-After": str(scan_quota.seconds_until_utc_midnight())},
        )


async def _refund_daily_scan(user: User, usage_day: date) -> None:
    """Give back a reservation; a failure here must not mask the original error."""
    try:
        async with AsyncSessionLocal() as quota_db:
            await scan_quota.refund_scan(quota_db, user.id, usage_day)
    except Exception:
        logger.exception("scan: failed to refund daily scan reservation")


@router.post("/scan", response_model=ScanResponse)
@limiter.limit(settings.rate_limit_scan)
async def scan(
    request: Request,
    file: UploadFile,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    cf_turnstile_response: str | None = Form(None, alias="cf-turnstile-response"),
    enhanced: bool = Form(False),
) -> ScanResponse:
    # --- Kill switches (env, no release needed) ---
    # Checked first: before the upload body is read, before Turnstile and
    # before any provider call.
    if not settings.scan_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_disabled",
        )
    if enhanced and not settings.scan_enhanced_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="enhanced_scan_disabled",
        )

    # --- Cloudflare Turnstile bot check ---
    # Enabled when TURNSTILE_REQUIRED=true (the default).
    # The startup handler already confirmed the secret key is present when required.
    if settings.turnstile_required:
        if not cf_turnstile_response:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Turnstile token",
            )
        if not await _verify_turnstile(cf_turnstile_response):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed",
            )

    # --- Validate extension and Content-Type BEFORE reading the body ---
    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File must be a JPEG, PNG, WebP, or HEIC image",
        )

    # Content-Type is in the multipart header — check it before reading bytes
    # so an invalid MIME type is rejected without consuming the upload body.
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File must be an image",
        )

    image_bytes = await file.read()

    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Image must be 5MB or smaller",
        )

    # Verify actual file content matches the declared extension.
    # Extension and Content-Type are both client-supplied and can be spoofed;
    # magic bytes confirm the payload is a real image before forwarding to OpenAI.
    if not validate_magic_bytes(ext, image_bytes):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File content does not match declared type",
        )

    # Antivirus scan (optional, controlled by CLAMAV_ENABLED setting).
    # Runs on the original bytes before any re-encoding.
    scan_for_malware(image_bytes)

    # Strip all metadata (EXIF/XMP/IPTC) and neutralise polyglot payloads by
    # re-encoding to JPEG.  Consistent with the data:image/jpeg type sent to OpenAI.
    image_bytes = sanitize_image(image_bytes)

    credits = current_user.enhanced_scan_credits or 0
    if enhanced and credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="no_enhanced_credits",
        )

    # --- Daily caps: global ceiling, then an exact per-user reservation ---
    usage_day = scan_quota.utc_today()
    await _reserve_daily_scan(current_user, usage_day)

    identifier = ChatGPTVisionIdentifier(enhanced=enhanced)
    # The reservation is given back unless the provider call returns normally.
    # An empty result still counts: the provider was paid for.
    refund = True
    try:
        candidates = await identifier.identify(image_bytes)
        refund = False
    except ScanUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_unavailable",
        ) from None
    finally:
        if refund:
            await _refund_daily_scan(current_user, usage_day)

    if not candidates:
        return ScanResponse(books=[], enhanced=enhanced, enhanced_scan_credits=credits)

    enrichment = EnrichmentService()
    enriched = await enrichment.enrich(candidates, limit=settings.scan_max_books)
    enriched = _dedupe_enriched(enriched)

    dedup = DeduplicationService()
    enriched = await dedup.check(db, str(current_user.id), enriched)

    # Charge only once there are books to hand back: an enhanced scan that
    # yields nothing — no candidates, or every lookup failed — is free.
    if enhanced and enriched:
        credits = await _spend_enhanced_credit(db, current_user)

    return ScanResponse(
        books=enriched, enhanced=enhanced, enhanced_scan_credits=credits
    )
