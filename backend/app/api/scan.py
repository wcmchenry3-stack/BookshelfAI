import logging
import uuid
from datetime import date
from functools import partial

import anyio
import anyio.to_thread
import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.ai import breaker, ledger, quota
from app.ai.gateway import (
    AIResult,
    FeatureDisabledError,
    ProviderTimeoutError,
    VisionPayload,
    resolve_spec,
    run_feature,
)
from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
from app.ai.registry import get_feature
from app.ai.settings import AISettings, AISettingsUnavailableError, get_ai_settings
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.file_security import sanitize_image, scan_for_malware
from app.core.file_validation import validate_magic_bytes
from app.core.limiter import limiter
from app.models.ai_usage import (
    STATUS_ERROR,
    STATUS_REFUNDED,
    STATUS_TIMEOUT,
)
from app.models.user import User
from app.schemas.book import EnrichedBook, ScanResponse
from app.schemas.scan import ScanUsageResponse
from app.services.book_identifier import ScanUnavailableError
from app.services.deduplication import DeduplicationService
from app.services.enrichment import EnrichmentService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])

MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5MB
# Upload read size; the handler never holds more than MAX_IMAGE_BYTES + 1 bytes.
READ_CHUNK_BYTES = 64 * 1024
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

# Image normalisation is CPU- and memory-heavy (a 40 MP image decodes to
# hundreds of MB), so it runs in a worker thread to keep the event loop free,
# and at most SCAN_NORMALISE_CONCURRENCY run at once per process to bound
# peak memory. Requests over the limit wait for a slot.
_NORMALISE_LIMITER = anyio.CapacityLimiter(settings.scan_normalise_concurrency)


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


async def _read_upload_capped(file: UploadFile, limit: int) -> bytes:
    """Read ``file`` in chunks; 413 as soon as more than ``limit`` bytes arrive.

    Each read asks for at most the bytes still allowed plus one, so no more
    than ``limit + 1`` bytes are ever read into memory.
    """
    buf = bytearray()
    while True:
        chunk = await file.read(min(READ_CHUNK_BYTES, limit + 1 - len(buf)))
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Image must be 5MB or smaller",
            )


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


async def _spend_enhanced_credit(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Atomically decrement the user's enhanced-scan credits; return the balance.

    The ``> 0`` guard makes two concurrent enhanced scans unable to drive the
    balance negative — the loser simply isn't charged.
    """
    result = await db.execute(
        update(User)
        .where(User.id == user_id, User.enhanced_scan_credits > 0)
        .values(enhanced_scan_credits=User.enhanced_scan_credits - 1)
        .returning(User.enhanced_scan_credits)
    )
    remaining = result.scalar_one_or_none()
    await db.commit()
    return int(remaining) if remaining is not None else 0


class ScanLimitReachedError(HTTPException):
    """402 ``scan_limit_reached``: the monthly allowance is used.

    Rendered by the handler in ``app.main`` as
    ``{"detail": "scan_limit_reached", "limit": N, "used": N,
    "period_end": "YYYY-MM-DD"}``, so ``detail`` stays the same string code
    every other scan error uses.
    """

    def __init__(self, limit: int, used: int, period_end: date) -> None:
        super().__init__(
            status_code=status.HTTP_402_PAYMENT_REQUIRED, detail="scan_limit_reached"
        )
        self.limit = limit
        self.used = used
        self.period_end = period_end

    def body(self) -> dict:
        return {
            "detail": self.detail,
            "limit": self.limit,
            "used": self.used,
            "period_end": self.period_end.isoformat(),
        }


async def _reserve_scan_quota(
    db: AsyncSession, user: User, feature: str, ai: AISettings
) -> quota.Reservation:
    """Global count cap, then the daily cost ceiling, then the in-flight
    lease, then the daily and monthly reservations (``app/ai/quota.py``),
    mapped to HTTP refusals.

    ``feature`` (the gateway feature) sets the lease length from its timeout
    and the cost estimate from its ``est_cost_ceiling_micros``; the units are reserved under ``quota.SCAN_QUOTA_FEATURE``, the
    one allowance standard and enhanced scans share.

    Runs on the request's own session; each step commits, so the connection
    is back in the pool during the provider call. A refusal has already given
    back anything taken.
    """
    spec = get_feature(feature)
    try:
        return await quota.reserve(
            db,
            user=user,
            feature=quota.SCAN_QUOTA_FEATURE,
            timeout_s=spec.timeout_s,
            ai=ai,
            est_cost_micros=spec.est_cost_ceiling_micros,
        )
    except quota.CostCeilingReached:
        # Free tier paused for the rest of the UTC day (#531, #544).
        logger.warning("scan: daily AI cost ceiling reached; free tier paused")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ai_budget_reached",
        ) from None
    except quota.GlobalCapacityReached:
        logger.warning("scan: global daily limit reached")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_capacity",
        ) from None
    except quota.ScanInProgress as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="scan_in_progress",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except quota.DailyLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="daily_scan_limit",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    except quota.MonthlyLimitReached as exc:
        raise ScanLimitReachedError(exc.limit, exc.used, exc.period_end) from None


async def _refund_scan_quota(db: AsyncSession, reservation: quota.Reservation) -> None:
    """Give back the daily and monthly units; never raises."""
    await quota.refund(db, reservation)


async def _release_scan_lease(
    db: AsyncSession,
    reservation: quota.Reservation,
    ledger_row: Executable | None = None,
    settlement: breaker.Settlement | None = None,
) -> None:
    """Release the in-flight lease (settle) and commit the call's usage-ledger
    row and cost settle in the same transaction; never raises, shielded."""
    await quota.release(db, reservation, ledger_row, settlement)


def _settlement(
    *,
    reservation: quota.Reservation,
    feature: str,
    ai: AISettings,
    status: str | None = None,
    result: AIResult | None = None,
) -> breaker.Settlement:
    """What this call adds to the day's spend (#531). With ``result`` (the
    provider answered) its priced cost; otherwise (``error`` or ``timeout``:
    real cost unknown) the reservation's estimate, so the breaker errs on the
    safe side.
    A call whose cost cannot be priced also settles the estimate. Never
    raises."""
    estimate = reservation.est_cost_micros
    if result is not None:
        try:
            cost = cost_micros(
                result.model,
                CURRENT_PRICE_VERSION,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
            )
        except Exception:
            logger.exception("scan: could not price the call; settling the estimate")
            cost = estimate
        return breaker.Settlement(cost, result.feature, result.model, result.outcome)
    try:
        model = resolve_spec(feature, ai).model
    except Exception:  # noqa: BLE001 - only an alert tag; never fail the settle
        model = "unknown"
    return breaker.Settlement(estimate, feature, model, status or STATUS_ERROR)


def _usage_row(
    *,
    call_id: uuid.UUID,
    user_id: uuid.UUID,
    feature: str,
    ai: AISettings,
    status: str | None = None,
    result: AIResult | None = None,
) -> Executable | None:
    """The ledger insert for this scan's provider call (#530), or None.

    With ``result`` (the provider answered) the status is its outcome
    (``ok``/``refused``/``truncated``) and the tokens are its usage; otherwise
    ``status`` is given and the tokens are 0. Never raises: a ledger problem
    (e.g. a model with no price) is logged and the row skipped, so it cannot
    turn a scan into an error or mask the original exception.
    """
    try:
        if result is not None:
            return ledger.usage_insert(
                call_id=call_id,
                user_id=user_id,
                feature=result.feature,
                model=result.model,
                status=result.outcome,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
                request_id=ledger.current_request_id(),
            )
        return ledger.usage_insert(
            call_id=call_id,
            user_id=user_id,
            feature=feature,
            # The same resolution run_feature used (same settings snapshot).
            model=resolve_spec(feature, ai).model,
            status=status,
            request_id=ledger.current_request_id(),
        )
    except Exception:
        logger.exception("scan: could not build the usage ledger row")
        return None


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
    # --- Kill switches (ai_settings table, no release needed) ---
    # Checked first: before the upload is read, Turnstile is called or any
    # provider is contacted. (The framework has already received the multipart
    # body by the time the handler runs.) If the settings cannot be read and
    # nothing is cached, scanning fails closed.
    try:
        ai = await get_ai_settings(db)
    except AISettingsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ai_unavailable",
        ) from None
    if not ai.scan_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="scan_disabled",
        )
    if enhanced and not ai.enhanced_scan_enabled:
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

    # Streaming cap: stop reading as soon as the upload passes 5MB.
    image_bytes = await _read_upload_capped(file, MAX_IMAGE_BYTES)

    # Verify actual file content matches the declared extension.
    # Extension and Content-Type are both client-supplied and can be spoofed;
    # magic bytes confirm the payload looks like the declared format before
    # anything tries to decode it.
    if not validate_magic_bytes(ext, image_bytes):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="File content does not match declared type",
        )

    # Antivirus scan (optional, controlled by CLAMAV_ENABLED setting).
    # Runs on the original bytes before any re-encoding.
    scan_for_malware(image_bytes)

    # The client only chooses standard vs enhanced; the gateway's registry
    # (with the ai_settings provider/model override) decides the provider,
    # model, token cap, timeout and image size for that feature.
    feature = "scan.enhanced" if enhanced else "scan.standard"

    # Normalise before any credit or quota check, so a bad image never uses a
    # scan: full decode (415 image_undecodable), pixel cap from
    # scan.max_image_pixels (413 image_too_large_pixels), EXIF orientation, all metadata stripped, long
    # edge capped for the feature, re-encoded as JPEG quality 85. Only these
    # bytes reach the provider.
    # Off the event loop; the 413/415 HTTPException raised in the thread
    # propagates out of run_sync unchanged.
    image_bytes = await anyio.to_thread.run_sync(
        partial(
            sanitize_image,
            image_bytes,
            max_long_edge_px=get_feature(feature).max_image_long_edge_px,
            max_pixels=ai.max_image_pixels,
        ),
        limiter=_NORMALISE_LIMITER,
    )

    # --- Entitlement: enhanced credits (#533 replaces this with its quota) ---
    credits = current_user.enhanced_scan_credits or 0
    if enhanced and credits <= 0:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="no_enhanced_credits",
        )

    # --- Tier allowance, global count cap, daily cost ceiling (#531),
    # in-flight lease, daily, monthly ---
    # One allowance for standard and enhanced scans (quota.SCAN_QUOTA_FEATURE).
    # At the cost ceiling Free gets 503 ai_budget_reached; Premium continues.
    # The tier is resolved server-side inside quota.reserve; every refusal
    # here happens before the provider and gives back what it took.
    # Read before anything can roll the session back: a failed give-back
    # (refund or lease release) rolls back, which expires current_user.
    user_id = current_user.id
    reservation = await _reserve_scan_quota(db, current_user, feature, ai)

    # The reservation is given back only when the provider reports itself
    # unavailable or was never reached (connection not established). Any other
    # failure (or a cancelled request) keeps it: the provider may already have
    # been paid. An empty result also counts. The lease is always released
    # once the provider call is over (settle), even on cancellation.
    #
    # Usage ledger (#530): every outcome of the provider call writes one
    # ai_usage row, committed with the lease release. call_id is generated
    # once per call, so a retried settle cannot write it twice. A kill switch
    # hit (FeatureDisabledError) never reached a provider and writes none.
    #
    # Cost (#531): the settle also moves the estimate out of today's reserved
    # spend. A refunded call (unavailable, never reached, kill switch)
    # settles nothing; an answered call settles its priced cost; a timed-out,
    # failed or cancelled call keeps the estimate as spend (real cost
    # unknown, likely billed).
    call_id = uuid.uuid4()
    row = partial(_usage_row, call_id=call_id, user_id=user_id, feature=feature, ai=ai)
    ledger_row: Executable | None = None
    # Cost settle (#531): None gives the estimate back (refunded calls).
    spend = partial(_settlement, reservation=reservation, feature=feature, ai=ai)
    settlement: breaker.Settlement | None = None
    ledger_decided = False
    try:
        try:
            result = await run_feature(
                feature,
                user=current_user,
                db=db,
                payload=VisionPayload(image_bytes=image_bytes),
                # The settings read above, so one request uses one view of them.
                settings_snapshot=ai,
            )
        except (ScanUnavailableError, httpx.ConnectError) as exc:
            # ConnectError: the connection to the provider was never
            # established, so nothing was billed; treat it like any other
            # unavailability.
            if isinstance(exc, ProviderTimeoutError):
                ledger_row = row(status=STATUS_TIMEOUT)
                # The request reached the provider and is likely billed even
                # though the user's units are refunded: keep the estimate as
                # spend (#531). Other unavailability gives it back.
                settlement = spend(status=STATUS_TIMEOUT)
            elif not isinstance(exc, FeatureDisabledError):
                ledger_row = row(status=STATUS_REFUNDED)
            ledger_decided = True
            await _refund_scan_quota(db, reservation)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="scan_unavailable",
            ) from None
        except Exception:
            # The reservation is kept (the provider may have been paid).
            ledger_row = row(status=STATUS_ERROR)
            settlement = spend(status=STATUS_ERROR)
            ledger_decided = True
            raise
        ledger_row = row(result=result)
        settlement = spend(result=result)
        ledger_decided = True
    finally:
        if not ledger_decided:
            # The call did not complete: the request was cancelled (or a
            # BaseException) mid-call. The reservation is kept, so record it
            # as an error with 0 tokens; the shielded settle writes it.
            ledger_row = row(status=STATUS_ERROR)
            settlement = spend(status=STATUS_ERROR)
        await _release_scan_lease(db, reservation, ledger_row, settlement)
    candidates = result.candidates

    if not candidates:
        return ScanResponse(books=[], enhanced=enhanced, enhanced_scan_credits=credits)

    enrichment = EnrichmentService()
    enriched = await enrichment.enrich(candidates, limit=settings.scan_max_books)
    enriched = _dedupe_enriched(enriched)

    dedup = DeduplicationService()
    enriched = await dedup.check(db, str(user_id), enriched)

    # Charge only once there are books to hand back: an enhanced scan that
    # yields nothing — no candidates, or every lookup failed — is free.
    if enhanced and enriched:
        credits = await _spend_enhanced_credit(db, user_id)

    return ScanResponse(
        books=enriched, enhanced=enhanced, enhanced_scan_credits=credits
    )


@router.get("/scan/usage", response_model=ScanUsageResponse)
@limiter.limit(settings.rate_limit_reads)
async def scan_usage(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScanUsageResponse:
    """The current user's scan usage and allowance (read only). Standard and
    enhanced scans count against the same allowance."""
    try:
        ai = await get_ai_settings(db)
    except AISettingsUnavailableError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ai_unavailable",
        ) from None
    usage = await quota.usage_for(
        db, user=current_user, feature=quota.SCAN_QUOTA_FEATURE, ai=ai
    )
    return ScanUsageResponse(
        used_today=usage.used_today,
        daily_limit=usage.daily_limit,
        used_month=usage.used_month,
        monthly_limit=usage.monthly_limit,
        period_end=usage.period_end,
    )
