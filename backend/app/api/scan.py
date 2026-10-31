"""``POST /scan`` and ``GET /scan/usage``. ``scan()`` runs named steps in a
fixed order; their helpers live in ``app/services/scan/`` (#808) and are
imported under the names tests patch on this module."""

import logging
import uuid
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
    run_feature,
)
from app.ai.registry import get_feature
from app.ai.settings import AISettings, AISettingsUnavailableError, get_ai_settings
from app.auth.dependencies import get_current_user
from app.core import catalog_token
from app.core.config import settings
from app.core.database import get_db
from app.core.file_security import sanitize_image
from app.core.limiter import limiter
from app.models.ai_usage import (
    STATUS_CACHE_HIT,
    STATUS_ERROR,
    STATUS_MALFORMED,
    STATUS_REFUNDED,
    STATUS_TIMEOUT,
)
from app.models.user import User
from app.schemas.book import EnrichedBook, ScanResponse
from app.schemas.scan import ScanUsageResponse
from app.services.book_identifier import ScanUnavailableError
from app.services.deduplication import DeduplicationService
from app.services.enrichment import EnrichmentService
from app.services.scan.books import dedupe_enriched as _dedupe_enriched
from app.services.scan.features import ENHANCED_FEATURE, STANDARD_FEATURE
from app.services.scan.quota_steps import count_scan_request as _count_scan_request
from app.services.scan.quota_steps import enhanced_remaining as _enhanced_remaining
from app.services.scan.quota_steps import no_match_field as _no_match_field
from app.services.scan.quota_steps import refund_scan_quota as _refund_scan_quota
from app.services.scan.quota_steps import release_scan_lease as _release_scan_lease
from app.services.scan.quota_steps import (
    require_enhanced_entitlement as _require_enhanced_entitlement,
)
from app.services.scan.quota_steps import reserve_scan_quota as _reserve_scan_quota
from app.services.scan.quota_steps import settle_decisions as _settle_decisions
from app.services.scan.records import lookup_record as _record_lookup
from app.services.scan.records import photo_fingerprint as _fingerprint
from app.services.scan.records import record_serves as _record_serves
from app.services.scan.records import recover_session as _recover_session
from app.services.scan.records import save_ai_step as _save_ai_step
from app.services.scan.records import save_enrichment_step as _save_enrichment_step
from app.services.scan.records import saved_record_id as _saved_record_id
from app.services.scan.settlement import build_settlement as _settlement
from app.services.scan.settlement import build_usage_row as _usage_row
from app.services.scan.uploads import MAX_IMAGE_BYTES
from app.services.scan.uploads import read_validated_upload as _read_validated_upload

logger = logging.getLogger(__name__)

router = APIRouter(tags=["scan"])

# The scan upload route. app.main's RequestSizeLimitMiddleware caps the whole
# request body on this path at MAX_SCAN_REQUEST_BYTES (#557): the image limit
# plus 1 MB for the multipart framing and the other form fields (the Turnstile
# token, ``enhanced``, ``attempt``), which together are a few KB.
SCAN_UPLOAD_PATH = "/scan"
MAX_SCAN_REQUEST_BYTES = MAX_IMAGE_BYTES + 1024 * 1024
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
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


# ---------------------------------------------------------------------------
# The steps of scan(), in the order it runs them
# ---------------------------------------------------------------------------


async def _load_scan_settings(db: AsyncSession, enhanced: bool) -> AISettings:
    """Kill switches (ai_settings table, no release needed). Checked first:
    before the upload is read, Turnstile is called or any provider is
    contacted. (The framework has already received the multipart body by the
    time the handler runs.) If the settings cannot be read and nothing is
    cached, scanning fails closed."""
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
    return ai


async def _check_turnstile(token: str | None) -> None:
    """Cloudflare Turnstile bot check, enabled when TURNSTILE_REQUIRED=true
    (the default). The startup handler already confirmed the secret key is
    present when required. 400 without a token, 403 when it fails."""
    if settings.turnstile_required:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Turnstile token",
            )
        if not await _verify_turnstile(token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed",
            )


async def _normalise_image(image_bytes: bytes, feature: str, ai: AISettings) -> bytes:
    """Normalise before any credit or quota check, so a bad image never uses a
    scan: full decode (415 image_undecodable), pixel cap from
    scan.max_image_pixels (413 image_too_large_pixels), EXIF orientation, all
    metadata stripped, long edge capped for the feature, re-encoded as JPEG
    quality 85. Only these bytes reach the provider.
    Off the event loop; the 413/415 HTTPException raised in the thread
    propagates out of run_sync unchanged."""
    return await anyio.to_thread.run_sync(
        partial(
            sanitize_image,
            image_bytes,
            max_long_edge_px=get_feature(feature).max_image_long_edge_px,
            max_pixels=ai.max_image_pixels,
        ),
        limiter=_NORMALISE_LIMITER,
    )


async def _serve_saved_answer(
    db: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    feature: str,
    enhanced: bool,
    fingerprint: str | None,
    ai: AISettings,
    attempt: int,
) -> ScanResponse | None:
    """Scan records (#585): the repeat response when this user has a record
    that answers the request, else None (the normal path runs). Looked up
    before the allowance, per user, by the SHA-256 of the normalised bytes
    only (not the prompt or model: old answers stay valid). A record with a
    finished AI step answers the request (see ``_record_serves``): no
    provider call and nothing taken; the enrichment step is redone only if
    it is missing. Feature gating still applies: an enhanced repeat for a
    non-Premium user (e.g. one who has since left Premium) gets the same
    403 enhanced_requires_premium as a new photo. Any scan-record failure
    takes the normal path."""
    record = await _record_lookup(db, user, user_id, fingerprint, ai, attempt=attempt)
    if (
        fingerprint is not None
        and record is not None
        and _record_serves(record, enhanced)
    ):
        tier = await _require_enhanced_entitlement(db, user, enhanced)
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
    return None


async def _run_provider(
    db: AsyncSession,
    user: User,
    *,
    user_id: uuid.UUID,
    reservation: quota.Reservation,
    call_id: uuid.UUID,
    feature: str,
    enhanced: bool,
    image_bytes: bytes,
    ai: AISettings,
    attempt: int,
) -> tuple[AIResult, quota.NoMatchMoved | None]:
    """Call the provider and settle (release the lease, write the ledger row
    and the cost) in the ``finally``; the answer and the no-match decision.
    Refunds the reservation and raises 503 ``scan_unavailable`` when the
    provider was unavailable or the reply malformed; any other failure
    propagates with the reservation kept. The full refund and settle policy
    is in ``app/services/scan/quota_steps.py``."""
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
    try:
        try:
            result = await run_feature(
                feature,
                user=user,
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
        zero_result, no_match = _settle_decisions(
            result, enhanced=enhanced, call_id=call_id, ledger_row=ledger_row
        )
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
    return result, moved


async def _record_and_enrich(
    db: AsyncSession,
    *,
    fingerprint: str | None,
    user_id: uuid.UUID,
    reservation: quota.Reservation,
    result: AIResult,
    ai: AISettings,
    attempt: int,
) -> tuple[list[EnrichedBook], uuid.UUID | None]:
    """After the settle and outside the lease: the scan record's AI step
    (#585), enrichment, the record's enrichment step once every lookup
    finished, the saved record's id (#631) and the library check. The
    books and the record id for the response. Never fails the scan on a
    scan-record problem."""
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
    return books, record_id


@router.post(SCAN_UPLOAD_PATH, response_model=ScanResponse)
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
    ai = await _load_scan_settings(db, enhanced)

    # --- /scan requests per user per UTC day (#586): after the kill
    # switches, before any other work (see _count_scan_request) ---
    await _count_scan_request(db, current_user, ai)
    await _check_turnstile(cf_turnstile_response)
    image_bytes = await _read_validated_upload(file)

    # The client only chooses standard vs enhanced; the gateway's registry
    # (with the ai_settings provider/model override) decides the provider,
    # model, token cap, timeout and image size for that feature.
    feature = ENHANCED_FEATURE if enhanced else STANDARD_FEATURE
    image_bytes = await _normalise_image(image_bytes, feature, ai)

    # Read before anything can roll the session back: a failed give-back
    # (refund or lease release) rolls back, which expires current_user.
    user_id = current_user.id
    fingerprint = _fingerprint(image_bytes, ai)
    repeat = await _serve_saved_answer(
        db,
        current_user,
        user_id=user_id,
        feature=feature,
        enhanced=enhanced,
        fingerprint=fingerprint,
        ai=ai,
        attempt=attempt,
    )
    if repeat is not None:
        return repeat

    # --- Enhanced entitlement (#533), tier allowance, global count cap,
    # daily cost ceiling (#531), in-flight lease, zero-result throttle, daily
    # (see _reserve_scan_quota) ---
    reservation = await _reserve_scan_quota(db, current_user, feature, ai, enhanced)

    # Generated server-side, once per provider call (#530): never from the
    # request.
    call_id = uuid.uuid4()
    result, moved = await _run_provider(
        db,
        current_user,
        user_id=user_id,
        reservation=reservation,
        call_id=call_id,
        feature=feature,
        enhanced=enhanced,
        image_bytes=image_bytes,
        ai=ai,
        attempt=attempt,
    )
    books, record_id = await _record_and_enrich(
        db,
        fingerprint=fingerprint,
        user_id=user_id,
        reservation=reservation,
        result=result,
        ai=ai,
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
    no monthly allowance (#582), so the body has no monthly fields."""
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
