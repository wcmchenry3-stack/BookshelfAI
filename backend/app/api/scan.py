import logging
import uuid
from datetime import datetime
from functools import partial
from typing import Annotated

import anyio
import anyio.to_thread
import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from app.ai import breaker, ledger, quota, scan_records
from app.ai.entitlements import Tier
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
from app.core import catalog_token
from app.core.config import settings
from app.core.database import get_db
from app.core.file_security import sanitize_image, scan_for_malware
from app.core.file_validation import validate_magic_bytes
from app.core.limiter import limiter
from app.models.ai_usage import (
    STATUS_CACHE_HIT,
    STATUS_ERROR,
    STATUS_MALFORMED,
    STATUS_OK,
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
STANDARD_FEATURE = "scan.standard"
ENHANCED_FEATURE = "scan.enhanced"
# The ``attempt`` form field (#603): 1 is the first try, 2..6 the app's
# scheduled retries (#574). Recorded only (ledger row and scan_record log
# line); never used for charging, limits or ordering.
SCAN_ATTEMPT_MIN = 1
SCAN_ATTEMPT_MAX = 6

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


async def _reserve_scan_quota(
    db: AsyncSession, user: User, feature: str, ai: AISettings, enhanced: bool
) -> quota.Reservation:
    """Global count cap, then the daily cost ceiling, then the in-flight
    lease, then the daily reservation (``app/ai/quota.py``), mapped to HTTP
    refusals. For ``enhanced`` (#533), first the Premium-only refusal (before
    anything is taken), then after the lease the zero-result throttle. There
    is no monthly reservation and no 402 (#582).

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
            enhanced=enhanced,
        )
    except quota.EnhancedRequiresPremium:
        raise _enhanced_requires_premium() from None
    except quota.EnhancedThrottled as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="enhanced_throttled",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
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


async def _count_scan_request(db: AsyncSession, user: User, ai: AISettings) -> None:
    """Count this /scan request for the user's UTC day (#586); 429
    ``scan_requests_daily_limit`` with a Retry-After until UTC midnight when
    the day's requests are used up. Distinct from the per-minute 429 and
    from ``daily_scan_limit``."""
    try:
        await quota.count_scan_request(db, user.id, ai)
    except quota.ScanRequestsDailyLimitReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="scan_requests_daily_limit",
            headers={"Retry-After": str(exc.retry_after)},
        ) from None


def _enhanced_requires_premium() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="enhanced_requires_premium",
    )


async def _require_enhanced_entitlement(
    db: AsyncSession, user: User, enhanced: bool
) -> Tier:
    """The user's tier (server-side); 403 ``enhanced_requires_premium`` for
    an enhanced scan by a non-Premium user (#533). Checked before a repeat
    is served from a scan record, so a repeat is gated like a new photo
    (#585); takes nothing."""
    try:
        return await quota.require_enhanced_entitlement(db, user, enhanced)
    except quota.EnhancedRequiresPremium:
        raise _enhanced_requires_premium() from None


# ---------------------------------------------------------------------------
# Scan records (#585)
# ---------------------------------------------------------------------------


def _fingerprint(image_bytes: bytes, ai: AISettings) -> str | None:
    """The photo's fingerprint (SHA-256 of the normalised bytes), or None
    when scan records are switched off. Never raises."""
    try:
        if not ai.scan_records_enabled:
            return None
        return scan_records.fingerprint(image_bytes)
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning("scan: fingerprint failed (%s)", type(exc).__name__)
        return None


async def _recover_session(db: AsyncSession, user: User | None = None) -> None:
    """Roll back after a scan-record failure and reload ``user`` (a rollback
    expires it), so the normal path can continue. Never raises."""
    try:
        await db.rollback()
        if user is not None:
            await db.refresh(user)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "scan: session recovery after a scan-record failure failed (%s)",
            type(exc).__name__,
        )


async def _record_lookup(
    db: AsyncSession,
    user: User,
    user_id: uuid.UUID,
    fingerprint: str | None,
    ai: AISettings,
    *,
    attempt: int,
) -> scan_records.Record | None:
    """This user's record for the photo, or None (no record, switched off,
    or any failure: logged without content, then the normal path runs)."""
    if fingerprint is None:
        return None
    try:
        return await scan_records.lookup(
            db,
            user_id=user_id,
            fingerprint=fingerprint,
            retention_days=ai.scan_records_retention_days,
        )
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning(
            "scan: scan-record lookup failed (%s); taking the normal path",
            type(exc).__name__,
        )
        scan_records.log_event(
            "lookup_failed", ai_done=False, enrich_done=False, attempt=attempt
        )
        await _recover_session(db, user)
        return None


def _record_serves(record: scan_records.Record | None, enhanced: bool) -> bool:
    """Whether ``record`` answers this request. A standard scan is answered
    by any record of the photo. An enhanced scan only by one an enhanced
    scan produced: an enhanced re-scan is how a user gets a better answer
    for the same photo (decision #573 point 5), so it never gets the
    standard answer back."""
    if record is None:
        return False
    return not enhanced or record.feature == ENHANCED_FEATURE


async def _save_ai_step(
    db: AsyncSession,
    *,
    fingerprint: str | None,
    user_id: uuid.UUID,
    reservation: quota.Reservation,
    result: AIResult,
    ai: AISettings,
    attempt: int,
) -> datetime | None:
    """Save the provider's answer as the record's AI step; the step's time,
    or None when not saved. Only outcome ``ok`` is saved, with books or
    without: since #572 the adapters report a missing, malformed or
    unparseable reply as ``malformed`` (a 503 that never reaches here), so an
    empty ``ok`` list is a genuine ``{"books": []}``, and a re-upload of that
    photo is a free repeat of the no-match (owner decision on #573). Never a
    refusal or a truncated reply with books (logged as ``not_saved``; one
    with no books is ``malformed``), nor an error. Never raises."""
    if fingerprint is None:
        return None
    if result.outcome != STATUS_OK:
        scan_records.log_event(
            "not_saved",
            ai_done=False,
            enrich_done=False,
            reason=result.outcome,
            attempt=attempt,
        )
        return None
    tier = reservation.tier.value if isinstance(reservation.tier, Tier) else "unknown"
    try:
        return await scan_records.save_ai_step(
            db,
            user_id=user_id,
            fingerprint=fingerprint,
            feature=result.feature,
            tier=tier,
            model=result.model,
            candidates=result.candidates,
            max_per_user=ai.scan_records_max_per_user,
            retention_days=ai.scan_records_retention_days,
        )
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning("scan: scan-record save failed (%s)", type(exc).__name__)
        scan_records.log_event(
            "save_failed", ai_done=False, enrich_done=False, attempt=attempt
        )
        await _recover_session(db)
        return None


async def _save_enrichment_step(
    db: AsyncSession,
    *,
    fingerprint: str | None,
    user_id: uuid.UUID,
    ai_created_at: datetime | None,
    books: list[EnrichedBook],
) -> bool:
    """Save the finished enrichment on the record; True when saved. Never
    raises."""
    if fingerprint is None or ai_created_at is None:
        return False
    try:
        return await scan_records.save_enrichment_step(
            db,
            user_id=user_id,
            fingerprint=fingerprint,
            ai_created_at=ai_created_at,
            books=books,
        )
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning(
            "scan: scan-record enrichment save failed (%s)", type(exc).__name__
        )
        await _recover_session(db)
        return False


async def _saved_record_id(
    db: AsyncSession,
    *,
    fingerprint: str | None,
    user_id: uuid.UUID,
    ai_created_at: datetime | None,
) -> uuid.UUID | None:
    """The id of the record just saved for this answer (#631), for
    ``ScanResponse.scan_record_id``; None when nothing was saved, the stored
    answer is another one, or on any failure. Never raises."""
    if fingerprint is None or ai_created_at is None:
        return None
    try:
        return await scan_records.record_id(
            db, user_id=user_id, fingerprint=fingerprint, ai_created_at=ai_created_at
        )
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning("scan: scan-record id read failed (%s)", type(exc).__name__)
        await _recover_session(db)
        return None


async def _serve_repeat(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    tier: Tier,
    feature: str,
    enhanced: bool,
    fingerprint: str,
    record: scan_records.Record,
    ai: AISettings,
    attempt: int,
) -> ScanResponse:
    """A repeat of a photo with a saved answer: no provider call and nothing
    taken (no reservation, lease, global count-cap unit, cost settle or
    zero-result row). One ``cache_hit`` ledger row (cost 0). A finished
    record is served as saved; one whose enrichment step is missing has
    only that step redone (and saved when it finishes). The library check
    always runs; the remaining enhanced allowance is read as it stands."""
    try:
        await ledger.record(
            db,
            call_id=uuid.uuid4(),
            user_id=user_id,
            feature=feature,
            model=record.model,
            status=STATUS_CACHE_HIT,
            request_id=ledger.current_request_id(),
            attempt=attempt,
        )
    except Exception as exc:  # noqa: BLE001 - never fail a repeat on the ledger
        logger.warning("scan: repeat ledger row failed (%s)", type(exc).__name__)
        await _recover_session(db)
    if record.books is not None:
        books = record.books
        event, enrich_done = "repeat", True
    else:
        books, complete = await _enrich(record.candidates)
        enrich_done = complete and await _save_enrichment_step(
            db,
            fingerprint=fingerprint,
            user_id=user_id,
            ai_created_at=record.created_at,
            books=books,
        )
        event = "repeat_enriched"
    books = await _mark_library(db, user_id, books)
    scan_records.log_event(
        event,
        ai_done=True,
        enrich_done=enrich_done,
        books=len(books),
        in_library=sum(1 for b in books if b.already_in_library),
        attempt=attempt,
    )
    try:
        remaining = await quota.enhanced_remaining_for(
            db, user_id=user_id, tier=tier, ai=ai
        )
    except Exception:
        logger.exception("scan: could not read the remaining enhanced allowance")
        await _recover_session(db)
        remaining = None
    return ScanResponse(
        books=catalog_token.sign_books(books, user_id),
        enhanced=enhanced,
        enhanced_scans_remaining=remaining,
        repeat=True,
        scan_record_id=record.id,
    )


async def _refund_scan_quota(db: AsyncSession, reservation: quota.Reservation) -> None:
    """Give back the daily unit; never raises."""
    await quota.refund(db, reservation)


async def _release_scan_lease(
    db: AsyncSession,
    reservation: quota.Reservation,
    ledger_row: Executable | None = None,
    settlement: breaker.Settlement | None = None,
    zero_result: quota.ZeroResult | None = None,
    no_match: quota.NoMatch | None = None,
) -> quota.NoMatchMoved | None:
    """Release the in-flight lease (settle) and commit the call's usage-ledger
    row, cost settle and (an enhanced scan with no books, #533) the charge
    decision or (a standard scan with no books, #577) the no-match move in
    the same transaction; never raises, shielded. Returns the no-match
    decision, if one was committed."""
    return await quota.release(
        db, reservation, ledger_row, settlement, zero_result, no_match
    )


async def _enrich(candidates: list) -> tuple[list[EnrichedBook], bool]:
    """Enrich and dedupe the provider's candidates (Open Library / Google
    Books); also whether every lookup finished (the scan record's
    enrichment step, #585)."""
    if not candidates:
        return [], True
    enrichment = EnrichmentService()
    enriched = await enrichment.enrich(candidates, limit=settings.scan_max_books)
    complete = getattr(enrichment, "last_complete", None) is True
    return _dedupe_enriched(enriched), complete


async def _mark_library(
    db: AsyncSession, user_id: uuid.UUID, books: list[EnrichedBook]
) -> list[EnrichedBook]:
    """Flag the books already in the user's library: ``ownership`` by
    edition, and ``already_in_library`` (#587)."""
    if not books:
        return []
    dedup = DeduplicationService()
    return await dedup.check(db, str(user_id), books)


async def _enhanced_remaining(
    db: AsyncSession, reservation: quota.Reservation
) -> int | None:
    """The remaining enhanced allowance for the response; None for
    non-Premium, or if it cannot be read (never fails the scan)."""
    try:
        return await quota.enhanced_remaining(db, reservation)
    except Exception:
        logger.exception("scan: could not read the remaining enhanced allowance")
        try:
            await db.rollback()
        except Exception:
            logger.exception("scan: rollback after the allowance read failed")
        return None


def _no_match_field(moved: object) -> str | None:
    """``ScanResponse.no_match`` from the settle's no-match decision (#577);
    None when there was none. Never raises."""
    if isinstance(moved, quota.NoMatchMoved) and moved.outcome in (
        quota.NO_MATCH_FREE,
        quota.NO_MATCH_COUNTED,
    ):
        return moved.outcome
    return None


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
    attempt: int,
) -> Executable | None:
    """The ledger insert for this scan's provider call (#530), or None.

    With ``result`` (the provider answered) the status is its outcome
    (``ok``/``refused``/``truncated``/``malformed``) and the tokens are its
    usage; otherwise
    ``status`` is given and the tokens are 0. ``attempt`` (#603) is the
    request's validated ``attempt`` field, stored as is. Never raises: a ledger problem
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
                attempt=attempt,
            )
        return ledger.usage_insert(
            call_id=call_id,
            user_id=user_id,
            feature=feature,
            # The same resolution run_feature used (same settings snapshot).
            model=resolve_spec(feature, ai).model,
            status=status,
            request_id=ledger.current_request_id(),
            attempt=attempt,
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
    # Which try this is (#603): 1 the first, 2..6 the app's scheduled
    # retries; missing means 1. Validated by FastAPI with the other form
    # fields, before the handler runs: anything else (not an integer, or out
    # of range) is a 422 with nothing counted, reserved or recorded, like a
    # bad ``enhanced``. Only recorded (each ledger row and the scan_record
    # log line); never used for charging, limits or ordering.
    attempt: Annotated[int, Form(ge=SCAN_ATTEMPT_MIN, le=SCAN_ATTEMPT_MAX)] = (
        SCAN_ATTEMPT_MIN
    ),
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

    # --- /scan requests per user per UTC day (#586) ---
    # Every request past authentication (get_current_user) and the kill
    # switches counts, new photo or repeat, allowed or refused later; nothing
    # is given back. At scan.requests_per_user_daily: 429
    # scan_requests_daily_limit until UTC midnight, before any other work
    # (Turnstile, the upload, normalisation, the scan-record lookup, the
    # allowance and the provider). The kill switches stay first: they cost
    # nothing (no DB write, no work), a switched-off scan should say so, and
    # the app's retries during an outage we caused should not use up a
    # user's requests. Turnstile comes after: it is an outbound call, i.e.
    # work this limit bounds, and a request that fails it is the kind of
    # request the limit is for. Repeats are free (#585), so this is what
    # stops someone hammering the endpoint (decision #573 point 3).
    await _count_scan_request(db, current_user, ai)

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
    feature = ENHANCED_FEATURE if enhanced else STANDARD_FEATURE

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

    # --- Enhanced entitlement (#533), tier allowance, global count cap,
    # daily cost ceiling (#531), in-flight lease, zero-result throttle,
    # daily ---
    # Enhanced scans are Premium-only: a non-Premium user gets 403
    # enhanced_requires_premium inside quota.reserve before anything is
    # reserved (the tier is resolved server-side there, never from the
    # request). Standard and enhanced scans share one daily allowance
    # (quota.SCAN_QUOTA_FEATURE); there is no monthly or enhanced monthly
    # allowance (#582), so nothing is refused for a monthly reason. At the
    # cost ceiling Free gets 503 ai_budget_reached; Premium continues. Every refusal here
    # happens before the provider and gives back what it took.
    # Read before anything can roll the session back: a failed give-back
    # (refund or lease release) rolls back, which expires current_user.
    user_id = current_user.id

    # --- Scan records (#585) ---
    # Looked up before the allowance, per user, by the SHA-256 of the
    # normalised bytes only (not the prompt or model: old answers stay
    # valid). A record with a finished AI step answers the request (see
    # _record_serves): no provider call and nothing taken; the enrichment
    # step is redone only if it is missing. Feature gating still applies:
    # an enhanced repeat for a non-Premium user (e.g. one who has since left
    # Premium) gets the same 403 enhanced_requires_premium as a new photo.
    # Any scan-record failure takes the normal path.
    fingerprint = _fingerprint(image_bytes, ai)
    record = await _record_lookup(
        db, current_user, user_id, fingerprint, ai, attempt=attempt
    )
    if (
        fingerprint is not None
        and record is not None
        and _record_serves(record, enhanced)
    ):
        tier = await _require_enhanced_entitlement(db, current_user, enhanced)
        return await _serve_repeat(
            db,
            user_id=user_id,
            tier=tier,
            feature=feature,
            enhanced=enhanced,
            fingerprint=fingerprint,
            record=record,
            ai=ai,
            attempt=attempt,
        )

    reservation = await _reserve_scan_quota(db, current_user, feature, ai, enhanced)

    # The reservation is given back only when the provider reports itself
    # unavailable or was never reached (connection not established). Any other
    # failure (or a cancelled request) keeps it: the provider may already have
    # been paid. An empty result also counts. The lease is always released
    # once the provider call is over (settle), even on cancellation.
    #
    # A malformed reply (#572: the provider answered HTTP 200, billed, but the
    # reply was missing, not JSON, of the wrong shape or ended on an
    # unexpected stop reason, or it was cut off by the token limit before a
    # single complete book) is treated like unavailability for the user:
    # 503 scan_unavailable and the daily unit refunded (the only unit, #582),
    # never a zero-result and never saved as a scan record.
    # Its real token usage is still recorded (ledger status ``malformed``)
    # and its priced cost settled, because the provider billed it.
    #
    # Enhanced zero-result record (#533): an enhanced scan takes only the
    # shared daily unit (#582). When the provider answered but returned no
    # books (no candidates), the settle records it in its own transaction
    # (quota.release with a ZeroResult): below the user's zero-result
    # threshold one ai_enhanced_zero_results row is written (the abuse
    # signal, with its warning and the throttle); the daily unit stays
    # charged either way.
    # The decision is on the provider's candidates, so enrichment stays after
    # the settle (the lease covers only the provider call); enrichment returns
    # a book for every candidate it does not crash on. A provider error,
    # timeout or unavailability is never a zero-result. A repeat served from
    # a scan record (#585) returned above: it made no provider call and
    # records none.
    #
    # Found / no-match (#577): a standard scan whose provider answered
    # properly (outcome ok) with no candidates passes a NoMatch to the
    # settle, which moves the found unit to the no-match counter when it
    # has room (in the settle transaction, under the same gate, at most
    # once). The response says what happened (``no_match``: ``free`` or
    # ``counted``). Enhanced scans, refusals and truncated replies never
    # move; malformed replies were already refunded above.
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
    row = partial(
        _usage_row,
        call_id=call_id,
        user_id=user_id,
        feature=feature,
        ai=ai,
        attempt=attempt,
    )
    ledger_row: Executable | None = None
    # Cost settle (#531): None gives the estimate back (refunded calls).
    spend = partial(_settlement, reservation=reservation, feature=feature, ai=ai)
    settlement: breaker.Settlement | None = None
    ledger_decided = False
    zero_result: quota.ZeroResult | None = None
    no_match: quota.NoMatch | None = None
    moved: quota.NoMatchMoved | None = None
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
        if result.outcome == STATUS_MALFORMED:
            # Billed but unusable (#572): refunded like unavailability; the
            # finally below settles the priced cost with the ledger row.
            await _refund_scan_quota(db, reservation)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="scan_unavailable",
            )
        # A refusal is still a zero-book answer here: refusals are out of
        # scope for #572, see the comment on #572 about the limits-matrix row
        # premium/enhanced/provider_refused_below_threshold. A truncated reply
        # with no books never gets here: the gateway reports it as
        # ``malformed`` (owner decision on #572).
        if enhanced and not result.candidates:
            # The provider answered with no books: the settle decides.
            zero_result = quota.ZeroResult(
                ledger_id=call_id if ledger_row is not None else None
            )
        if not enhanced and result.outcome == STATUS_OK and not result.candidates:
            # A genuine no-match (#577): the settle decides the move.
            no_match = quota.NoMatch()
    finally:
        if not ledger_decided:
            # The call did not complete: the request was cancelled (or a
            # BaseException) mid-call. The reservation is kept, so record it
            # as an error with 0 tokens; the shielded settle writes it.
            ledger_row = row(status=STATUS_ERROR)
            settlement = spend(status=STATUS_ERROR)
        moved = await _release_scan_lease(
            db, reservation, ledger_row, settlement, zero_result, no_match
        )

    # Scan record (#585), step by step after the settle and outside the
    # lease: the AI step first, then the enrichment step once every lookup
    # finished. Never fails the scan.
    ai_saved_at = await _save_ai_step(
        db,
        fingerprint=fingerprint,
        user_id=user_id,
        reservation=reservation,
        result=result,
        ai=ai,
        attempt=attempt,
    )
    books, complete = await _enrich(result.candidates)
    enrich_done = False
    if ai_saved_at is not None:
        enrich_done = complete and await _save_enrichment_step(
            db,
            fingerprint=fingerprint,
            user_id=user_id,
            ai_created_at=ai_saved_at,
            books=books,
        )
    record_id = await _saved_record_id(
        db, fingerprint=fingerprint, user_id=user_id, ai_created_at=ai_saved_at
    )
    books = await _mark_library(db, user_id, books)
    if ai_saved_at is not None:
        scan_records.log_event(
            "saved",
            ai_done=True,
            enrich_done=enrich_done,
            books=len(books),
            in_library=sum(1 for b in books if b.already_in_library),
            attempt=attempt,
        )

    return ScanResponse(
        books=catalog_token.sign_books(books, user_id),
        enhanced=enhanced,
        enhanced_scans_remaining=await _enhanced_remaining(db, reservation),
        no_match=_no_match_field(moved),
        scan_record_id=record_id,
    )


@router.get("/scan/usage", response_model=ScanUsageResponse)
@limiter.limit(settings.rate_limit_reads)
async def scan_usage(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ScanUsageResponse:
    """The current user's scan usage and daily allowances (read only): the
    found and no-match counters (#577). Standard and enhanced scans count
    against the same found allowance; ``used_today`` / ``daily_limit`` are
    the found counter, kept for one release for older app builds. There is
    no monthly allowance (#582): the monthly fields are always null (kept
    for one release so older app builds still parse the body)."""
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
        found_used_today=usage.used_today,
        found_daily_limit=usage.daily_limit,
        no_match_used_today=usage.no_match_used_today,
        no_match_daily_limit=usage.no_match_daily_limit,
    )
