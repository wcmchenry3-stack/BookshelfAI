"""Per-user scan records by photo fingerprint (#585, decision #573).

Every photo is fingerprinted: the SHA-256 of the normalised JPEG (the bytes
the provider would get, ``app.core.file_security.sanitize_image``), so only
the exact same file matches. Per user there is at most one record per
fingerprint (unique ``(user_id, fingerprint)``) holding the provider's
answer, the enrichment result and which of those steps finished.

Key
---
The user (from the session) and the fingerprint (from the bytes after
normalisation) only. Not the prompt, the model or the request shape: an old
answer stays valid when either changes (decision #573 point 5). Nothing in
the request names a record; :func:`lookup` always filters on the user.

Steps
-----
1. ``ai_done``: :func:`save_ai_step` upserts the validated candidate list
   (title, author, confidence, ISBN-13, ISBN-10; never the raw model output)
   with the feature, tier and model that produced it. A new answer for the
   same photo (an enhanced scan of a photo first scanned as standard)
   replaces the old one and clears the enrichment step.
2. ``enrich_done``: :func:`save_enrichment_step` stores the enriched books
   (Open Library / Google Books data, without the per-user library flag,
   which is re-checked on every scan). It only lands on the answer it was
   computed from (matched on the AI step's ``created_at``).

The caller decides what is saved: only a provider outcome ``ok`` with at
least one candidate (never a refusal, a truncated reply or any error), and an
enrichment only when every lookup finished.

Lifetime
--------
A record is served and kept for ``scan_records.retention_days`` (default 30)
after ``created_at`` (the time of the AI step): :func:`lookup` filters on
it, so a lowered setting applies at once, and the daily cleanup task deletes
older rows in bounded batches (:func:`purge_old`). Each user keeps at most
``scan_records.max_per_user`` rows; :func:`save_ai_step` evicts that user's
oldest rows beyond the cap (bounded per call). Rows go with the user (ON
DELETE CASCADE).

Idempotence: both saves are keyed on ``(user_id, fingerprint)``; running one
again (a commit whose acknowledgement was lost, then a retry) leaves the
same single row.

Callers (``/scan``) must treat any exception here as "no record" or a
skipped save: scan records never fail a scan. This module never calls a
provider and must not import ``app.ai.providers``.
"""

import hashlib
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import settings as ai_settings
from app.core.config import settings
from app.models.scan_record import ScanRecord
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate

logger = logging.getLogger(__name__)

# Rows evicted per save at most; a lowered cap converges over later saves.
EVICT_BATCH_SIZE = 1_000
# Same bounded-batch pattern as the other purges.
PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20
# What a saved candidate may hold, and how long its text fields may be.
_CANDIDATE_FIELDS = ("title", "author", "confidence", "isbn_13", "isbn_10")
MAX_TEXT_LENGTH = 500
# What a saved enriched book may hold: the EnrichedBook fields except the
# per-user ones (book_id is always None on a scan; the library flag is live).
_BOOK_FIELDS = frozenset(EnrichedBook.model_fields) - {"book_id", "already_in_library"}


class ScanRecordError(ValueError):
    """A stored row does not hold a valid candidate list or book list."""


@dataclass(frozen=True)
class Record:
    """A saved scan record, validated."""

    feature: str
    tier: str
    model: str
    candidates: list[BookCandidate]
    # None until the enrichment step finished.
    books: list[EnrichedBook] | None
    created_at: datetime

    @property
    def enrich_done(self) -> bool:
        return self.books is not None


def utc_now() -> datetime:
    return datetime.now(UTC)


def fingerprint(image_bytes: bytes) -> str:
    """SHA-256 hex of the normalised image bytes."""
    return hashlib.sha256(image_bytes).hexdigest()


def max_items() -> int:
    """The most candidates or books a record may hold: the scan's own cap
    (``SCAN_MAX_BOOKS``), on write and on read."""
    return settings.scan_max_books


# ---------------------------------------------------------------------------
# Serialisation (validated both ways)
# ---------------------------------------------------------------------------


def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        raise ScanRecordError(f"bad {field}")
    return value


def _candidate(item: Any) -> BookCandidate:
    if not isinstance(item, dict) or set(item) - set(_CANDIDATE_FIELDS):
        raise ScanRecordError("bad candidate")
    confidence = item.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise ScanRecordError("bad confidence")
    return BookCandidate(
        title=_text(item.get("title"), "title"),
        author=_text(item.get("author"), "author"),
        confidence=float(confidence),
        isbn_13=_text(item.get("isbn_13"), "isbn_13", optional=True),
        isbn_10=_text(item.get("isbn_10"), "isbn_10", optional=True),
    )


def serialise_candidates(candidates: list[BookCandidate]) -> list[dict[str, Any]]:
    """The validated candidates as JSON: exactly the candidate fields."""
    items = [
        {
            "title": c.title,
            "author": c.author,
            "confidence": c.confidence,
            "isbn_13": c.isbn_13,
            "isbn_10": c.isbn_10,
        }
        for c in candidates
    ]
    # Validate on the way in too, so nothing unexpected is ever stored.
    deserialise_candidates(items)
    return items


def deserialise_candidates(value: Any) -> list[BookCandidate]:
    """A stored candidate list; :class:`ScanRecordError` if invalid."""
    if not isinstance(value, list) or len(value) > max_items():
        raise ScanRecordError("bad candidates")
    return [_candidate(item) for item in value]


def serialise_books(books: list[EnrichedBook]) -> list[dict[str, Any]]:
    """The enriched books as JSON, without the per-user fields."""
    items = [b.model_dump(mode="json", include=set(_BOOK_FIELDS)) for b in books]
    deserialise_books(items)
    return items


def deserialise_books(value: Any) -> list[EnrichedBook]:
    """A stored book list (``already_in_library`` False; the caller checks
    the library); :class:`ScanRecordError` if invalid."""
    if not isinstance(value, list) or len(value) > max_items():
        raise ScanRecordError("bad books")
    books = []
    for item in value:
        if not isinstance(item, dict) or set(item) - _BOOK_FIELDS:
            raise ScanRecordError("bad book")
        try:
            books.append(EnrichedBook.model_validate(item))
        except ValidationError:
            raise ScanRecordError("bad book") from None
    return books


# ---------------------------------------------------------------------------
# Reads and writes
# ---------------------------------------------------------------------------


def _cutoff(now: datetime, retention_days: int) -> datetime:
    return now - timedelta(days=retention_days)


async def lookup(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    fingerprint: str,
    retention_days: int,
    now: datetime | None = None,
) -> Record | None:
    """This user's record for ``fingerprint`` with a finished AI step,
    younger than ``retention_days``; else None. A row that does not validate
    is None (the AI step is redone); an enrichment that does not validate is
    treated as missing. Ends its read transaction (commit), so no connection
    is left idle in a transaction."""
    query = select(ScanRecord).where(
        ScanRecord.user_id == user_id,
        ScanRecord.fingerprint == fingerprint,
        ScanRecord.ai_done.is_(True),
        ScanRecord.created_at > _cutoff(now or utc_now(), retention_days),
    )
    row = (await db.execute(query)).scalar_one_or_none()
    await db.commit()
    if not isinstance(row, ScanRecord):
        return None
    try:
        candidates = deserialise_candidates(row.candidates)
    except ScanRecordError:
        return None
    books = None
    if row.enrich_done:
        try:
            books = deserialise_books(row.enrichment)
        except ScanRecordError:
            books = None
    return Record(
        feature=row.feature,
        tier=row.tier,
        model=row.model,
        candidates=candidates,
        books=books,
        created_at=row.created_at,
    )


async def save_ai_step(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    fingerprint: str,
    feature: str,
    tier: str,
    model: str,
    candidates: list[BookCandidate],
    max_per_user: int,
    now: datetime | None = None,
) -> datetime:
    """Upsert the AI step (clearing any enrichment), then evict the user's
    oldest rows beyond ``max_per_user`` (at most :data:`EVICT_BATCH_SIZE`);
    one commit. Returns the step's ``created_at``, which
    :func:`save_enrichment_step` matches on. Idempotent by (user,
    fingerprint)."""
    created = now or utc_now()
    values = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "fingerprint": fingerprint,
        "feature": feature,
        "tier": tier,
        "model": model,
        "candidates": serialise_candidates(candidates),
        "enrichment": None,
        "ai_done": True,
        "enrich_done": False,
        "created_at": created,
        "updated_at": created,
    }
    stmt = insert(ScanRecord).values(**values)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_scan_records_user_fingerprint",
        set_={
            key: stmt.excluded[key]
            for key in (
                "feature",
                "tier",
                "model",
                "candidates",
                "enrichment",
                "ai_done",
                "enrich_done",
                "created_at",
                "updated_at",
            )
        },
    )
    beyond_cap = (
        select(ScanRecord.id)
        .where(ScanRecord.user_id == user_id)
        .order_by(ScanRecord.created_at.desc(), ScanRecord.id.desc())
        .offset(max_per_user)
        .limit(EVICT_BATCH_SIZE)
    )
    try:
        await db.execute(stmt)
        await db.execute(
            delete(ScanRecord).where(
                ScanRecord.user_id == user_id, ScanRecord.id.in_(beyond_cap)
            )
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return created


async def save_enrichment_step(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    fingerprint: str,
    ai_created_at: datetime,
    books: list[EnrichedBook],
    now: datetime | None = None,
) -> bool:
    """Mark the enrichment step done with ``books`` on the record whose AI
    step was saved at ``ai_created_at`` (so it never lands on a newer
    answer); one commit. True when a row was updated. Idempotent."""
    stmt = (
        update(ScanRecord)
        .where(
            ScanRecord.user_id == user_id,
            ScanRecord.fingerprint == fingerprint,
            ScanRecord.ai_done.is_(True),
            ScanRecord.created_at == ai_created_at,
        )
        .values(
            enrichment=serialise_books(books),
            enrich_done=True,
            updated_at=now or utc_now(),
        )
    )
    try:
        result = await db.execute(stmt)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return bool(result.rowcount)


async def purge_old(
    db: AsyncSession, retention_days: int, now: datetime | None = None
) -> int:
    """Delete records older than ``retention_days`` in bounded batches;
    return how many."""
    cutoff = _cutoff(now or utc_now(), retention_days)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(ScanRecord.id)
            .where(ScanRecord.created_at <= cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(delete(ScanRecord).where(ScanRecord.id.in_(batch)))
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total


async def purge_old_scan_records(db: AsyncSession, now: datetime | None = None) -> int:
    """The daily purge with ``scan_records.retention_days``. If the settings
    cannot be read, the upper bound (30 days, the most a record may be kept)
    is used, so a record is never kept longer than allowed."""
    try:
        retention = (await ai_settings.get_ai_settings(db)).scan_records_retention_days
    except Exception as exc:  # noqa: BLE001 - fall back to the strictest bound
        logger.warning(
            "scan_records: settings unreadable for the purge (%s); using %d days",
            type(exc).__name__,
            ai_settings.SCAN_RECORDS_RETENTION_DAYS_BOUNDS[1],
        )
        await db.rollback()
        retention = ai_settings.SCAN_RECORDS_RETENTION_DAYS_BOUNDS[1]
    return await purge_old(db, retention, now)


# ---------------------------------------------------------------------------
# Observability (#585 point 8): counts only, no user ID, no content
# ---------------------------------------------------------------------------


def log_event(
    event: str,
    *,
    ai_done: bool,
    enrich_done: bool,
    books: int = 0,
    in_library: int = 0,
    reason: str | None = None,
) -> None:
    """One structured line per scan that touched a record, so the rate of
    records left with an incomplete step (and of repeats whose books are not
    in the library) can be counted from the logs. Events:

    - ``saved``: a new answer was saved (``enrich_done`` false: the
      enrichment step is left incomplete);
    - ``repeat``: a finished record was served;
    - ``repeat_enriched``: an AI-done record had its enrichment redone;
    - ``not_saved``: an answered scan was not saved (``reason``: the
      outcome, or ``no_match`` for an empty list, see ``/scan``);
    - ``save_failed`` / ``lookup_failed``: a database error (normal path).
    """
    logger.info(
        "scan_record event=%s ai_done=%s enrich_done=%s books=%d in_library=%d "
        "reason=%s",
        event,
        str(ai_done).lower(),
        str(enrich_done).lower(),
        books,
        in_library,
        reason or "-",
    )
