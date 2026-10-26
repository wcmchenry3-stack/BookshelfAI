"""Scan correction flags (#631): the server decides, the client only names
a candidate, and no text is ever stored.

Database-free (the CI security job has no Postgres): a fake session runs
the unverified add and answers the scan-record lookup the way the SQL
would, returning the record only when the statement is bound to its owner
and its id. The same flows run on real Postgres in
``tests/integration/test_scan_corrections_db.py``.

- Another user's ``scan_record_id`` records nothing, and the response is
  the same (status, and the body apart from the new row's ids and times) as
  a request with no reference, so the endpoint cannot probe for records.
- The title, author and ISBN the client sent never reach the new tables:
  only server-derived booleans and the record's feature, tier and model.
- The new tables have no String/Text/JSON columns except feature, tier and
  model, and the daily table has no user column.
- The client cannot set the flags (extra fields are 422), and a non-UUID
  ``scan_record_id`` or a negative index is 422.

Run on its own with:  pytest -m security tests/security/test_scan_corrections_authority.py
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, String, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Insert, Select

from app.main import app
from app.models.scan_correction import ScanCorrection, ScanCorrectionDaily
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook

pytestmark = pytest.mark.security

NOW = datetime.now(UTC)
ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000631a1"), email="a@x.io")
BOB_ID = uuid.UUID("00000000-0000-0000-0000-0000000631b0")
RECORD_ID = uuid.UUID("00000000-0000-0000-0000-00000063100d")
CANDIDATES = [
    {
        "title": "The Read Title",
        "author": "Read Author",
        "confidence": 0.8,
        "isbn_13": None,
        "isbn_10": None,
    }
]
NEW_TABLES = {"scan_corrections", "scan_corrections_daily"}


class FakeSession:
    """Runs the unverified add (no existing entries) and answers the scan
    record lookup only when its statement is bound to ``owner`` and the
    record's id, as the SQL would. Records every insert into the new
    tables."""

    def __init__(self, owner):
        self.owner = owner
        self.added: list = []
        self.inserts: list = []
        self.commits = 0

    async def execute(self, stmt):
        result = MagicMock()
        result.all.return_value = []
        result.one_or_none.return_value = None
        result.scalar_one_or_none.return_value = None
        if isinstance(stmt, Select) and any(
            getattr(f, "name", None) == "scan_records" for f in stmt.get_final_froms()
        ):
            params = stmt.compile(dialect=postgresql.dialect()).params.values()
            if self.owner in params and RECORD_ID in params:
                result.one_or_none.return_value = SimpleNamespace(
                    id=RECORD_ID,
                    feature="scan.standard",
                    tier="free",
                    model="vision-model",
                    candidates=CANDIDATES,
                    created_at=NOW,
                )
        elif isinstance(stmt, Insert) and stmt.table.name in NEW_TABLES:
            self.inserts.append(stmt)
            result.scalar_one_or_none.return_value = len(self.inserts)
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, UserBook):
            obj.id = obj.id or uuid.uuid4()
            obj.created_at = obj.updated_at = NOW
            obj.custom_book = next(
                o for o in self.added if isinstance(o, UserCustomBook)
            )
            obj.custom_book.created_at = obj.custom_book.updated_at = NOW


@pytest.fixture
def post():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    def _post(body, *, owner=ALICE.id):
        db = FakeSession(owner)

        async def _db():
            yield db

        app.dependency_overrides[get_current_user] = lambda: ALICE
        app.dependency_overrides[get_db] = _db
        return TestClient(app).post("/wishlist/unverified", json=body), db

    yield _post
    app.dependency_overrides.clear()


def _ref(index=0, record_id=RECORD_ID):
    return {"scan_record_id": str(record_id), "candidate_index": index}


def _shape(value):
    """The body with the new row's ids and times blanked."""
    if isinstance(value, dict):
        return {
            k: (
                "*"
                if k in {"id", "created_at", "updated_at", "wishlisted_at"}
                else _shape(v)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_shape(v) for v in value]
    return value


BODY = {"title": "Typed Title", "author": "Typed Author", "isbn": "0306406152"}


def test_own_record_is_recorded(post):
    """Control for the tests below: the fake answers the owner's lookup."""
    resp, db = post({**BODY, "scan_reference": _ref()})
    assert resp.status_code == 201
    assert {s.table.name for s in db.inserts} == NEW_TABLES


def test_another_users_record_records_nothing_and_answers_the_same(post):
    plain, _ = post(BODY)
    foreign, db = post({**BODY, "scan_reference": _ref()}, owner=BOB_ID)

    assert db.inserts == []
    assert foreign.status_code == plain.status_code == 201
    assert foreign.headers["content-type"] == plain.headers["content-type"]
    assert _shape(foreign.json()) == _shape(plain.json())


@pytest.mark.parametrize(
    "ref",
    [_ref(index=1), _ref(index=999), _ref(record_id=uuid.uuid4())],
    ids=["index-out-of-range", "index-at-max", "unknown-record"],
)
def test_unusable_reference_records_nothing_and_answers_the_same(post, ref):
    plain, _ = post(BODY)
    resp, db = post({**BODY, "scan_reference": ref})
    assert db.inserts == []
    assert resp.status_code == plain.status_code
    assert _shape(resp.json()) == _shape(plain.json())


def test_client_text_never_reaches_the_new_tables(post):
    resp, db = post({**BODY, "scan_reference": _ref()})
    assert resp.status_code == 201
    values = [
        v
        for stmt in db.inserts
        for v in stmt.compile(dialect=postgresql.dialect()).params.values()
    ]
    strings = [v for v in values if isinstance(v, str)]
    # Only the record's feature and model (and tier on the raw row).
    assert set(strings) <= {"scan.standard", "free", "vision-model"}
    for typed in ("Typed", "Title", "Author", "0306406152", "9780306406157"):
        assert not any(typed in s for s in strings)
    # Nor the scan's own reading.
    assert not any("Read" in s for s in strings)


@pytest.mark.parametrize("model", [ScanCorrection, ScanCorrectionDaily])
def test_new_tables_hold_no_text_except_feature_tier_and_model(model):
    textual = {
        c.name
        for c in model.__table__.columns
        if isinstance(c.type, String | Text | JSON | postgresql.JSONB)
    }
    assert textual <= {"feature", "tier", "model"}
    assert "user_id" not in model.__table__.columns


@pytest.mark.parametrize(
    "extra",
    [
        {"title_edited": False},
        {"author_edited": False},
        {"isbn_edited": True},
        {"any_edited": False},
        {"feature": "scan.enhanced"},
        {"model": "x"},
        {"candidate_index": 0},
        {"scan_record_id": str(RECORD_ID)},
    ],
)
def test_client_cannot_set_the_flags(post, extra):
    resp, db = post({**BODY, **extra})
    assert resp.status_code == 422
    assert db.added == [] and db.inserts == []
    resp, db = post({**BODY, "scan_reference": {**_ref(), **extra}})
    if extra.keys() <= {"candidate_index", "scan_record_id"}:
        return  # the reference's own fields
    assert resp.status_code == 422
    assert db.added == [] and db.inserts == []


@pytest.mark.parametrize(
    "ref",
    [
        {"scan_record_id": "not-a-uuid", "candidate_index": 0},
        {"scan_record_id": "1", "candidate_index": 0},
        {"scan_record_id": str(RECORD_ID), "candidate_index": -1},
        {"scan_record_id": str(RECORD_ID), "candidate_index": 1.5},
        {"scan_record_id": str(RECORD_ID)},
        {"scan_record_id": None, "candidate_index": 0},
    ],
)
def test_malformed_reference_is_refused(post, ref):
    resp, db = post({**BODY, "scan_reference": ref})
    assert resp.status_code == 422
    assert db.added == [] and db.inserts == []
