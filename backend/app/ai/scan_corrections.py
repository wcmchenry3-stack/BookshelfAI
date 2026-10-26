"""How often users correct the AI's reading of a scan (#631).

When the user saves an "Add it yourself" entry (``POST /wishlist/unverified``)
pre-filled from a scan result, the app sends only a reference: the scan
record's id and the candidate's index (``ScanResponse.scan_record_id``,
``EnrichedBook.candidate_index``), never its own copy of the pre-filled
values. The server loads the candidate it stored itself
(``scan_records.candidates``), scoped to the session's user, and records one
boolean each for whether the saved title, author and ISBN differ from what
the form was pre-filled with. No text is stored, neither the reading nor
what the user typed.

What counts as an edit
----------------------
The form is pre-filled as the app does it (``draftFromBook`` in
``frontend/lib/unverified.ts``) from the enriched book. "Add it yourself" is
offered only for a book no provider matched, so that book's title and author
are the candidate's reading, verbatim (one author string, however many
authors it names), and its ISBN is empty: enrichment keeps only ISBNs a
provider record confirmed (``enrichment._edition_isbns``), and there is no
provider record. :func:`prefill_from_candidate` mirrors that.

The comparison is after normalisation: title with ``normalise_title``,
author with ``normalise_text`` (case, punctuation, whitespace and, for the
title, a leading article are not edits), the ISBN as an ISBN-13
(``normalise_user_isbn`` / ``normalise_isbn_13``: hyphens, spaces and the
ISBN-10 form are not edits). An ISBN added, removed or changed is an edit.

Silently ignored (the entry is saved, nothing is recorded, the response is
the same as without a reference): a record that does not exist, expired,
belongs to another user or does not validate, and an index out of range.
Recording never fails the save: :func:`record_quietly` swallows any error.

Storage and rollup
------------------
One ``scan_corrections`` row per (record, candidate); a second save of the
same candidate inserts nothing (ON CONFLICT DO NOTHING) and counts nothing.
The row is counted into ``scan_corrections_daily`` (UTC day, feature, model;
no user) in the SAME transaction that inserts it. That is the rollup: the
raw rows cascade-delete with their scan record, which can go well before 30
days (``scan_records.retention_days`` may be as low as 1, the per-user cap
evicts, account deletion), so a rollup of 30-day-old rows could lose them.
Counting at write time cannot lose or double-count a row. The ledger's daily
rollup (``app.ai.ledger.rollup_and_purge``) then only deletes raw rows older
than its 30 days and daily rows older than its 400 days, which is idempotent.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import scan_records
from app.ai import settings as ai_settings
from app.models.scan_correction import ScanCorrection, ScanCorrectionDaily
from app.schemas.user_book import normalise_user_isbn
from app.services.book_identifier import BookCandidate
from app.services.enrichment import normalise_isbn_13, normalise_text, normalise_title

logger = logging.getLogger(__name__)

# The app's form limits (UNVERIFIED_LIMITS in frontend/lib/unverified.ts):
# it cuts the pre-filled title and author to these lengths.
_PREFILL_MAX = 500


@dataclass(frozen=True)
class Prefill:
    """What the "Add it yourself" form showed: title, author, ISBN or None."""

    title: str
    author: str
    isbn: str | None


@dataclass(frozen=True)
class Flags:
    title_edited: bool
    author_edited: bool
    isbn_edited: bool

    @property
    def any_edited(self) -> bool:
        return self.title_edited or self.author_edited or self.isbn_edited


def prefill_from_candidate(candidate: BookCandidate) -> Prefill:
    """The form's starting values for a book enriched from ``candidate``
    with no provider match: the reading's title and author (cut as the app
    cuts them) and no ISBN (see the module docstring)."""
    return Prefill(
        title=candidate.title[:_PREFILL_MAX],
        author=candidate.author[:_PREFILL_MAX],
        isbn=None,
    )


def _text_key(value: str, normalise) -> str:
    # Text of only punctuation normalises to "": compare it casefolded, so
    # "!!!" -> "???" is still an edit (as the unverified duplicate key does).
    return normalise(value) or value.strip().casefold()


def _isbn_key(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        return normalise_user_isbn(value)
    except ValueError:
        compact = value.replace("-", "").replace(" ", "").upper()
        return normalise_isbn_13(compact, compact) or compact


def compare(prefill: Prefill, *, title: str, author: str, isbn: str | None) -> Flags:
    """Which fields the saved entry changed from ``prefill``."""
    return Flags(
        title_edited=_text_key(prefill.title, normalise_title)
        != _text_key(title, normalise_title),
        author_edited=_text_key(prefill.author, normalise_text)
        != _text_key(author, normalise_text),
        isbn_edited=_isbn_key(prefill.isbn) != _isbn_key(isbn),
    )


def _utc_day(ts: datetime) -> date:
    return ts.astimezone(UTC).date()


async def record(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    scan_record_id: uuid.UUID,
    candidate_index: int,
    title: str,
    author: str,
    isbn: str | None,
    now: datetime | None = None,
) -> bool:
    """Record the saved entry's correction flags against this user's
    candidate and count them into the day's totals; one commit. True when a
    row was added; False when the reference names nothing usable or the
    candidate was already recorded. May raise (database errors)."""
    retention = (await ai_settings.get_ai_settings(db)).scan_records_retention_days
    found = await scan_records.lookup_by_id(
        db, user_id=user_id, record_id=scan_record_id, retention_days=retention
    )
    if found is None or not 0 <= candidate_index < len(found.candidates):
        return False
    flags = compare(
        prefill_from_candidate(found.candidates[candidate_index]),
        title=title,
        author=author,
        isbn=isbn,
    )
    created = now or datetime.now(UTC)
    inserted = (
        await db.execute(
            insert(ScanCorrection)
            .values(
                scan_record_id=scan_record_id,
                candidate_index=candidate_index,
                feature=found.feature,
                tier=found.tier,
                model=found.model,
                title_edited=flags.title_edited,
                author_edited=flags.author_edited,
                isbn_edited=flags.isbn_edited,
                created_at=created,
            )
            .on_conflict_do_nothing(constraint="uq_scan_corrections_record_candidate")
            .returning(ScanCorrection.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        await db.commit()
        return False
    counts = {
        "prefilled_saves": 1,
        "title_edited": int(flags.title_edited),
        "author_edited": int(flags.author_edited),
        "isbn_edited": int(flags.isbn_edited),
        "any_edited": int(flags.any_edited),
    }
    daily = insert(ScanCorrectionDaily).values(
        day=_utc_day(created), feature=found.feature, model=found.model, **counts
    )
    daily = daily.on_conflict_do_update(
        index_elements=[
            ScanCorrectionDaily.day,
            ScanCorrectionDaily.feature,
            ScanCorrectionDaily.model,
        ],
        set_={
            name: getattr(ScanCorrectionDaily, name) + daily.excluded[name]
            for name in counts
        },
    )
    await db.execute(daily)
    # The row and its count commit together.
    await db.commit()
    return True


async def record_quietly(db: AsyncSession, **fields) -> None:
    """:func:`record`, never raising: a failure is logged (the error type
    only, no content) and rolled back. The entry is already saved."""
    try:
        await record(db, **fields)
    except Exception as exc:  # noqa: BLE001 - never fail the save
        logger.warning("scan_corrections: not recorded (%s)", type(exc).__name__)
        try:
            await db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001
            logger.warning(
                "scan_corrections: rollback failed (%s)", type(rollback_exc).__name__
            )
