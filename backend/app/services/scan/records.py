"""Scan records (#585) for the scan route: the photo's fingerprint, the
lookup, whether a record answers the request, and the step-by-step saves.
None of these ever fails a scan: a failure is logged without content, the
session is recovered and the normal path runs."""

import logging
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import quota, scan_records
from app.ai.entitlements import Tier
from app.ai.gateway import AIResult
from app.ai.settings import AISettings
from app.models.ai_usage import STATUS_OK
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.scan import SCAN_LOGGER
from app.services.scan.features import ENHANCED_FEATURE

logger = logging.getLogger(SCAN_LOGGER)


def photo_fingerprint(image_bytes: bytes, ai: AISettings) -> str | None:
    """The photo's fingerprint (SHA-256 of the normalised bytes), or None
    when scan records are switched off. Never raises."""
    try:
        if not ai.scan_records_enabled:
            return None
        return scan_records.fingerprint(image_bytes)
    except Exception as exc:  # noqa: BLE001 - scan records never fail a scan
        logger.warning("scan: fingerprint failed (%s)", type(exc).__name__)
        return None


async def recover_session(db: AsyncSession, user: User | None = None) -> None:
    """Roll back after a scan-record failure and reload ``user`` (a rollback
    expires it), so the normal path can continue. Never raises."""
    try:
        await db.rollback()
        if user is not None:
            await db.refresh(user)
    except Exception as exc:  # noqa: BLE001 - best-effort recovery
        logger.warning(
            "scan: session recovery after a scan-record failure failed (%s)",
            type(exc).__name__,
        )


async def lookup_record(
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
        await recover_session(db, user)
        return None


def record_serves(record: scan_records.Record | None, enhanced: bool) -> bool:
    """Whether ``record`` answers this request. A standard scan is answered
    by any record of the photo. An enhanced scan only by one an enhanced
    scan produced: an enhanced re-scan is how a user gets a better answer
    for the same photo (decision #573 point 5), so it never gets the
    standard answer back."""
    if record is None:
        return False
    return not enhanced or record.feature == ENHANCED_FEATURE


async def save_ai_step(
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
        await recover_session(db)
        return None


async def save_enrichment_step(
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
        await recover_session(db)
        return False


async def saved_record_id(
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
        await recover_session(db)
        return None
