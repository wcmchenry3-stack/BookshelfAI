"""The account export document (#694): one JSON object holding a copy of
everything the app keeps about one user.

Layout (``schema_version`` :data:`SCHEMA_VERSION`; bump it when a field
changes meaning or goes away; adding a table or column does not)::

    {
      "schema_version": 1,
      "generated_at": "<ISO 8601 UTC>",
      "user_id": "<uuid>",
      "identities": [{"provider": "google", "subject": "<sub>"}],
      "tables": {"<table>": [{<column>: <value>, ...}, ...], ...},
      "catalog": {"books": [...], "editions": [...]},
      "excluded_tables": {"<table>": "<why it is not exported>", ...}
    }

``tables`` has one list per table in :data:`SECTIONS`, every row the user
owns, trashed library entries included (the user's data until the trash
purge removes it; ``deleted_at`` says so). Each section names the columns
it exports and every column it leaves out with the reason; secrets (token
ids and families, request hashes, device public keys, photo fingerprints)
are never exported. ``catalog`` holds the shared-catalog rows the user's
library points at, so the export can be read without the app; it is
reference data, not the user's, and is read only.

Tables with a foreign key to ``users`` (directly or through another such
table) that are not exported are in :data:`EXCLUDED_TABLES` with a reason.
``tests/unit/test_account_export_document.py`` fails when a new table or
column is neither exported nor excluded, so nothing is missed silently.
Account deletion (#291) can use the same lists.

Reads only, through the ORM; values become JSON (UUIDs as strings, times
in ISO 8601). Never logs a value.
"""

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_export import AccountExport
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import AIQuota
from app.models.ai_usage import AIUsage
from app.models.book import Book
from app.models.device_key import DeviceKey
from app.models.edition import Edition
from app.models.refresh_token import RefreshToken
from app.models.scan_correction import ScanCorrection
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.services import library_events

SCHEMA_VERSION = 1

Loader = Callable[[AsyncSession, uuid.UUID], Awaitable[Sequence[Any]]]

# The owner column every section leaves out: it is the export's ``user_id``.
_OWNER = {"user_id": "the owner: the document's user_id"}


@dataclass(frozen=True)
class Section:
    """One table of the export: what it exports, what it leaves out and
    why, and how it reads the user's rows."""

    columns: tuple[str, ...]
    load: Loader
    excluded: dict[str, str] = field(default_factory=dict)


def _owned(model, *order) -> Loader:
    async def load(db: AsyncSession, user_id: uuid.UUID) -> Sequence[Any]:
        rows = await db.execute(
            select(model).where(model.user_id == user_id).order_by(*order)
        )
        return list(rows.scalars())

    return load


async def _user(db: AsyncSession, user_id: uuid.UUID) -> Sequence[Any]:
    user = await db.get(User, user_id)
    return [user] if user is not None else []


async def _scan_corrections(db: AsyncSession, user_id: uuid.UUID) -> Sequence[Any]:
    # The user is reachable through the scan record only.
    rows = await db.execute(
        select(ScanCorrection)
        .join(ScanRecord, ScanRecord.id == ScanCorrection.scan_record_id)
        .where(ScanRecord.user_id == user_id)
        .order_by(ScanCorrection.created_at, ScanCorrection.id)
    )
    return list(rows.scalars())


SECTIONS: dict[str, Section] = {
    "users": Section(
        columns=(
            "id",
            "email",
            "display_name",
            "avatar_url",
            "default_tab",
            "ai_consent_at",
            "ai_consent_withdrawn_at",
            "created_at",
            "updated_at",
        ),
        load=_user,
        excluded={
            "google_id": "exported under identities",
            "token_version": "internal session-revocation counter",
        },
    ),
    "user_books": Section(
        columns=(
            "id",
            "book_id",
            "custom_book_id",
            "edition_id",
            "status",
            "wishlisted_at",
            "purchased_at",
            "started_at",
            "finished_at",
            "notes",
            "rating",
            "title_override",
            "author_override",
            "cover_url_override",
            "deleted_at",
            "created_at",
            "updated_at",
            "version",
        ),
        load=_owned(UserBook, UserBook.created_at, UserBook.id),
        excluded=_OWNER,
    ),
    "user_custom_books": Section(
        columns=(
            "id",
            "title",
            "author",
            "isbn_13",
            "deleted_at",
            "created_at",
            "updated_at",
            "version",
        ),
        load=_owned(UserCustomBook, UserCustomBook.created_at, UserCustomBook.id),
        excluded=_OWNER,
    ),
    "scan_records": Section(
        columns=(
            "id",
            "feature",
            "tier",
            "model",
            "candidates",
            "enrichment",
            "ai_done",
            "enrich_done",
            "created_at",
            "updated_at",
        ),
        load=_owned(ScanRecord, ScanRecord.created_at, ScanRecord.id),
        excluded={
            **_OWNER,
            "fingerprint": "a SHA-256 of the photo, an internal matching key",
        },
    ),
    "scan_corrections": Section(
        columns=(
            "id",
            "scan_record_id",
            "candidate_index",
            "feature",
            "tier",
            "model",
            "title_edited",
            "author_edited",
            "isbn_edited",
            "created_at",
        ),
        load=_scan_corrections,
    ),
    "ai_enhanced_zero_results": Section(
        columns=("id", "created_at", "warned"),
        load=_owned(
            AIEnhancedZeroResult,
            AIEnhancedZeroResult.created_at,
            AIEnhancedZeroResult.id,
        ),
        excluded={**_OWNER, "ledger_id": "internal link to a provider call id"},
    ),
    "ai_quota": Section(
        columns=("feature", "window_kind", "window_start", "units_used"),
        load=_owned(AIQuota, AIQuota.window_start, AIQuota.feature),
        excluded=_OWNER,
    ),
    "ai_usage": Section(
        columns=(
            "id",
            "feature",
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "status",
            "attempt",
            "created_at",
        ),
        load=_owned(AIUsage, AIUsage.created_at, AIUsage.id),
        excluded={
            **_OWNER,
            "call_id": "internal provider-call id",
            "request_id": "internal request correlation id",
            "price_version": "internal price table version",
            "cost_micros": "the provider's cost to the service, not the user's data",
        },
    ),
    "library_events": Section(
        columns=("id", "user_book_id", "event_type", "changed_fields", "created_at"),
        load=library_events.history,
        excluded={**_OWNER, "request_id": "internal request correlation id"},
    ),
    "device_keys": Section(
        columns=("id", "platform", "last_used_at", "created_at"),
        load=_owned(DeviceKey, DeviceKey.created_at, DeviceKey.id),
        excluded={
            **_OWNER,
            "key_id": "the attestation key identifier (a device credential)",
            "public_key_cbor": "the device's attestation public key",
            "counter": "the attestation replay counter",
        },
    ),
    "refresh_tokens": Section(
        columns=("issued_at", "expires_at", "revoked_at"),
        load=_owned(RefreshToken, RefreshToken.issued_at, RefreshToken.id),
        excluded={
            **_OWNER,
            "id": "identifies a session credential",
            "jti": "identifies a session credential",
            "family_id": "identifies a session credential",
            "used_at": "internal rotation state",
            "replaced_by_id": "identifies a session credential",
            "displaced_at": "internal rotation state",
        },
    ),
    "account_exports": Section(
        columns=("id", "status", "created_at", "completed_at", "expires_at"),
        load=_owned(AccountExport, AccountExport.created_at, AccountExport.id),
        excluded={
            **_OWNER,
            "locale": "internal: the language of the email",
            "attempts": "internal worker state",
            "claimed_at": "internal worker state",
            "object_path": "internal storage location",
            "size_bytes": "internal",
            "error_code": "internal worker state",
        },
    ),
}

# Tables owned by a user that the export leaves out, and why.
EXCLUDED_TABLES: dict[str, str] = {
    "idempotency_keys": (
        "transient: stored responses for retries, kept 24 hours, holding "
        "request hashes and copies of data exported elsewhere"
    ),
    "ai_inflight": "transient: a lease held for the seconds a scan runs",
}

# Shared-catalog rows the library points at (reference data, read only).
CATALOG_BOOK_COLUMNS = (
    "id",
    "title",
    "author",
    "open_library_work_id",
    "google_books_id",
    "cover_url",
    "language",
)
CATALOG_EDITION_COLUMNS = (
    "id",
    "book_id",
    "isbn_13",
    "isbn_10",
    "publisher",
    "publish_year",
    "format",
    "page_count",
)


def to_json(value: Any) -> Any:
    """A column value as JSON."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _row(obj: Any, columns: Sequence[str]) -> dict[str, Any]:
    return {name: to_json(getattr(obj, name)) for name in columns}


async def _catalog(db: AsyncSession, entries: Sequence[Any]) -> dict[str, list]:
    book_ids = {e.book_id for e in entries if e.book_id is not None}
    edition_ids = {e.edition_id for e in entries if e.edition_id is not None}
    books: list = []
    editions: list = []
    if book_ids:
        rows = await db.execute(
            select(Book).where(Book.id.in_(book_ids)).order_by(Book.id)
        )
        books = [_row(b, CATALOG_BOOK_COLUMNS) for b in rows.scalars()]
    if edition_ids:
        rows = await db.execute(
            select(Edition).where(Edition.id.in_(edition_ids)).order_by(Edition.id)
        )
        editions = [_row(e, CATALOG_EDITION_COLUMNS) for e in rows.scalars()]
    return {"books": books, "editions": editions}


async def build(
    db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None
) -> dict[str, Any]:
    """The export document of ``user_id``."""
    loaded = {name: await s.load(db, user_id) for name, s in SECTIONS.items()}
    users = loaded["users"]
    identities = [{"provider": "google", "subject": u.google_id} for u in users]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "user_id": str(user_id),
        "identities": identities,
        "tables": {
            name: [_row(obj, SECTIONS[name].columns) for obj in rows]
            for name, rows in loaded.items()
        },
        "catalog": await _catalog(db, loaded["user_books"]),
        # Said in the file, so the user can see what is left out and why.
        "excluded_tables": dict(EXCLUDED_TABLES),
    }
