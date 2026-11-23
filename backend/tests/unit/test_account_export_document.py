"""The account export covers every user table, and nothing secret (#694).

- Enumeration: every table that holds a user's data (``tests/_user_tables.py``:
  a foreign key to ``users``, directly or through another such table) is
  either exported (``SECTIONS``) or excluded with a reason
  (``EXCLUDED_TABLES``), and nothing else is exported as a user table. A
  new table fails here until someone decides.
- Columns: each exported table lists every one of its columns, either
  exported or left out with a reason, so a new column is decided too.
- Secrets: token ids and families, request hashes, device keys and photo
  fingerprints are never exported.
- The document itself, built from fake rows: ``schema_version`` first,
  every section present, values as JSON.

Real rows (a seeded user and a second user) are in
``tests/integration/test_account_export_db.py``.
"""

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.core.database import Base
from app.services import account_export
from app.services.account_export import EXCLUDED_TABLES, SECTIONS
from tests._user_tables import user_owned_tables

# Never in an export, whatever table they are in.
SECRET_COLUMNS = {
    "jti",
    "family_id",
    "replaced_by_id",
    "key",
    "key_id",
    "request_hash",
    "response_body",
    "public_key_cbor",
    "token_version",
    "fingerprint",
    "lease_id",
}


def test_every_user_table_is_exported_or_excluded():
    owned = user_owned_tables()
    decided = set(SECTIONS) | set(EXCLUDED_TABLES)
    missing = sorted(owned - decided)
    assert missing == [], (
        "tables holding user data that the export neither exports nor "
        f"excludes: {missing}. Add a Section to app/services/account_export.py "
        "SECTIONS, or a reason to EXCLUDED_TABLES."
    )


def test_only_user_tables_are_sections_and_none_is_both():
    owned = user_owned_tables()
    assert set(SECTIONS) <= owned
    assert set(EXCLUDED_TABLES) <= owned
    assert not set(SECTIONS) & set(EXCLUDED_TABLES)
    assert all(reason.strip() for reason in EXCLUDED_TABLES.values())


def test_the_enumeration_sees_indirect_ownership():
    # scan_corrections has no user_id; it reaches the user via scan_records.
    assert "scan_corrections" in user_owned_tables()
    assert "books" not in user_owned_tables()
    assert "ai_usage_daily" not in user_owned_tables()


@pytest.mark.parametrize("table", sorted(SECTIONS))
def test_every_column_is_exported_or_excluded(table):
    section = SECTIONS[table]
    columns = {c.name for c in Base.metadata.tables[table].columns}
    exported, excluded = set(section.columns), set(section.excluded)
    assert not exported & excluded, table
    assert exported | excluded == columns, (
        f"{table}: undecided {sorted(columns - exported - excluded)}, "
        f"unknown {sorted((exported | excluded) - columns)}"
    )
    assert all(reason.strip() for reason in section.excluded.values())


def test_no_secret_column_is_exported():
    for table, section in SECTIONS.items():
        leaked = SECRET_COLUMNS & set(section.columns)
        assert not leaked, (table, leaked)
    assert "user_id" not in {c for s in SECTIONS.values() for c in s.columns}


def test_catalog_columns_exist():
    books = {c.name for c in Base.metadata.tables["books"].columns}
    editions = {c.name for c in Base.metadata.tables["editions"].columns}
    assert set(account_export.CATALOG_BOOK_COLUMNS) <= books
    assert set(account_export.CATALOG_EDITION_COLUMNS) <= editions


def test_to_json_converts_column_values():
    uid = uuid.uuid4()
    when = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    assert account_export.to_json(uid) == str(uid)
    assert account_export.to_json(when) == "2026-11-23T12:00:00+00:00"
    assert account_export.to_json(date(2026, 11, 23)) == "2026-11-23"
    assert account_export.to_json(Decimal("1.5")) == "1.5"
    assert account_export.to_json({"a": [1]}) == {"a": [1]}


def _fake_row(table: str, **overrides) -> SimpleNamespace:
    values = {c.name: f"{table}.{c.name}" for c in Base.metadata.tables[table].columns}
    values.update(overrides)
    return SimpleNamespace(**values)


async def test_build_lays_out_every_section(monkeypatch):
    uid = uuid.uuid4()
    entry = _fake_row("user_books", book_id=None, edition_id=None)
    rows = {name: [_fake_row(name)] for name in SECTIONS}
    rows["user_books"] = [entry]
    rows["users"] = [_fake_row("users", google_id="sub-1")]
    sections = {
        name: account_export.Section(
            columns=s.columns,
            excluded=s.excluded,
            load=lambda _db, _uid, name=name: _rows(rows[name]),
        )
        for name, s in SECTIONS.items()
    }
    monkeypatch.setattr(account_export, "SECTIONS", sections)
    now = datetime(2026, 11, 23, tzinfo=UTC)
    doc = await account_export.build(object(), uid, now)

    assert next(iter(doc)) == "schema_version"
    assert doc["schema_version"] == account_export.SCHEMA_VERSION == 1
    assert doc["user_id"] == str(uid)
    assert doc["generated_at"] == now.isoformat()
    assert doc["identities"] == [{"provider": "google", "subject": "sub-1"}]
    assert set(doc["tables"]) == set(SECTIONS)
    assert doc["catalog"] == {"books": [], "editions": []}
    assert doc["excluded_tables"] == EXCLUDED_TABLES
    for name, (row,) in doc["tables"].items():
        assert list(row) == list(SECTIONS[name].columns)
    text = json.dumps(doc)
    for column in SECRET_COLUMNS:
        assert f'.{column}"' not in text, column


async def _rows(rows):
    return rows
