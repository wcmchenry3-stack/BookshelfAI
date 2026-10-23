"""Library ownership (#587): only the requesting user's library decides
``ownership`` on scan / search results and the 409 on ``POST /wishlist``,
and a refused add writes nothing.

Database-free (the CI security job has no Postgres): a small fake session
answers library queries only for the user they are scoped to, so a query
that forgot ``user_id`` (or used another user's) would see nothing. The
same flows run on real Postgres in
``tests/integration/test_library_ownership_db.py``.

Run on its own with:  pytest -m security tests/security/test_library_ownership.py
"""

import io
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai.providers.base import ProviderResult
from app.main import app
from app.models.book import Book
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from app.services.deduplication import DeduplicationService
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

pytestmark = pytest.mark.security

NOW = datetime.now(UTC)

ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000587a1"), email="a@x.io")
BOB = User(id=uuid.UUID("00000000-0000-0000-0000-0000000587b2"), email="b@x.io")
WORK = "OL45804W"
DUNE_13 = "9780441013593"
DUNE_10 = "0441013597"
OTHER_13 = "9780593099322"


def _entry(isbn_13=None, isbn_10=None, *, no_edition=False):
    edition = None if no_edition else SimpleNamespace(isbn_13=isbn_13, isbn_10=isbn_10)
    return SimpleNamespace(edition=edition, edition_id=uuid.uuid4())


class FakeLibrarySession:
    """Answers library (``user_books``) queries from ``library`` — one
    owner's entries of the work ``WORK`` — and catalog lookups from
    ``book``. A library query sees the entries only when it is scoped to
    the owner (the owner's id is one of its bound parameters)."""

    def __init__(self, owner: User, library: list, book=None):
        self.owner = owner
        self.library = library
        self.book = book
        self.added: list = []
        self.committed = False
        self.rolled_back = False

    def _scoped_to_owner(self, stmt) -> bool:
        values = stmt.compile().params.values()
        return any(v in (self.owner.id, str(self.owner.id)) for v in values)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        result = MagicMock()
        if entity is Book:
            result.scalar_one_or_none.return_value = self.book
            return result
        rows = self.library if self._scoped_to_owner(stmt) else []
        result.scalars.return_value.all.return_value = rows
        result.scalar_one_or_none.return_value = rows[0] if rows else None
        return result

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, UserBook):
            obj.id = obj.id or uuid.uuid4()
            obj.created_at = obj.updated_at = NOW
            obj.book, obj.edition = self.book, None
        elif isinstance(obj, Book):
            obj.editions = []


def _result(isbn_13=DUNE_13, isbn_10=None) -> EnrichedBook:
    editions = (
        [EditionPreview(isbn_13=isbn_13, isbn_10=isbn_10)] if isbn_13 or isbn_10 else []
    )
    return EnrichedBook(
        open_library_work_id=WORK,
        title="Dune",
        author="Frank Herbert",
        confidence=0.9,
        editions=editions,
    )


# ─── Scan / search results ───────────────────────────────────────────────────


class TestResultsMarkedFromOwnLibraryOnly:
    @pytest.mark.parametrize(
        "library, result, expected",
        [
            ([_entry(DUNE_13)], _result(), "owned_same_edition"),
            ([_entry(None, DUNE_10)], _result(), "owned_same_edition"),
            ([_entry(DUNE_13)], _result(None, DUNE_10), "owned_same_edition"),
            ([_entry(OTHER_13)], _result(), "owned_other_edition"),
            ([_entry(no_edition=True)], _result(), "owned_edition_unknown"),
            ([_entry(DUNE_13)], _result(None), "owned_edition_unknown"),
        ],
        ids=[
            "same_isbn",
            "isbn10_owned_isbn13_result",
            "isbn13_owned_isbn10_result",
            "other_edition",
            "owned_without_edition",
            "result_without_isbn",
        ],
    )
    async def test_owner_sees_ownership(self, library, result, expected):
        db = FakeLibrarySession(ALICE, library)
        (book,) = await DeduplicationService().check(db, str(ALICE.id), [result])
        assert book.ownership == expected
        assert book.already_in_library is True

    async def test_another_users_library_does_not_count(self):
        db = FakeLibrarySession(ALICE, [_entry(DUNE_13)])
        (book,) = await DeduplicationService().check(db, str(BOB.id), [_result()])
        assert book.ownership == "not_owned"
        assert book.already_in_library is False


# ─── POST /wishlist ──────────────────────────────────────────────────────────


@pytest.fixture
def wishlist():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    state: dict = {}

    async def _db():
        yield state["db"]

    def post(body, *, user, db):
        state["db"] = db
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = _db
        return TestClient(app).post("/wishlist", json=body)

    yield post
    app.dependency_overrides.clear()


def _body(isbn_13=DUNE_13, isbn_10=None) -> dict:
    editions = [{"isbn_13": isbn_13, "isbn_10": isbn_10}] if isbn_13 or isbn_10 else []
    return {
        "open_library_work_id": WORK,
        "title": "Dune",
        "author": "Frank Herbert",
        "editions": editions,
    }


def _existing_book() -> Book:
    return Book(
        id=uuid.uuid4(),
        open_library_work_id=WORK,
        title="D",
        author="F",
        language="en",
        created_at=NOW,
        updated_at=NOW,
    )


class TestWishlistRefusesOwnedBooks:
    @pytest.mark.parametrize(
        "library, body, code",
        [
            ([_entry(DUNE_13)], _body(), "owned_same_edition"),
            ([_entry(DUNE_13)], _body(None, DUNE_10), "owned_same_edition"),
            ([_entry(OTHER_13)], _body(), "owned_other_edition"),
            ([_entry(no_edition=True)], _body(), "owned_edition_unknown"),
            ([_entry(DUNE_13)], _body(None), "owned_edition_unknown"),
        ],
        ids=["same_isbn", "isbn10_vs_isbn13", "other", "no_edition", "no_isbn"],
    )
    def test_409_with_the_code_and_nothing_written(self, wishlist, library, body, code):
        db = FakeLibrarySession(ALICE, library, book=_existing_book())
        resp = wishlist(body, user=ALICE, db=db)
        assert resp.status_code == 409
        assert resp.json() == {"detail": code}
        assert db.added == [] and db.committed is False and db.rolled_back is True

    def test_another_users_library_does_not_block_the_add(self, wishlist):
        # Alice has the book; Bob adding it is not refused for that.
        db = FakeLibrarySession(ALICE, [_entry(DUNE_13)], book=_existing_book())
        with patch(
            "app.services.wishlist.WishlistService._find_or_create_edition",
            AsyncMock(return_value=None),
        ):
            resp = wishlist(_body(), user=BOB, db=db)
        assert resp.status_code == 201, resp.text
        assert db.committed is True
        (user_book,) = db.added
        assert user_book.user_id == BOB.id


# ─── Scan repeats (#585) ─────────────────────────────────────────────────────


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (5, 8, 7)).save(buf, format="JPEG")
    return buf.getvalue()


class TestScanRepeatChecksTheLibraryAfresh:
    def test_repeat_reflects_the_library_now_and_record_holds_no_ownership(self):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        store, records = FakeQuotaStore(), FakeScanRecords()
        library: list = []
        fake = FakeLibrarySession(ALICE, library)
        real = DeduplicationService()
        image = _jpeg()

        async def _fake_db():
            yield AsyncMock()

        async def _enrich(candidates, limit):
            enrich.return_value.last_complete = True
            return [_result() for _ in candidates]

        app.dependency_overrides[get_current_user] = lambda: ALICE
        app.dependency_overrides[get_db] = _fake_db
        try:
            with (
                store.installed(),
                records.installed(),
                patch("app.ai.gateway.OpenAIVisionProvider") as cls,
                patch("app.api.scan.EnrichmentService") as enrich,
                patch("app.api.scan.DeduplicationService") as dedup,
                patch("app.api.scan.ledger.record", AsyncMock()),
            ):
                cls.return_value.identify = AsyncMock(
                    return_value=ProviderResult(
                        [BookCandidate(title="Dune", author="F", confidence=0.9)]
                    )
                )
                enrich.return_value.enrich = AsyncMock(side_effect=_enrich)

                # The real check, against the fake library.
                async def _check(_db, uid, books):
                    return await real.check(fake, uid, books)

                dedup.return_value.check = AsyncMock(side_effect=_check)

                def post():
                    return TestClient(app).post(
                        "/scan",
                        files={"file": ("s.jpg", io.BytesIO(image), "image/jpeg")},
                    )

                first = post().json()
                library.append(_entry(OTHER_13))  # Alice adds another edition
                repeat = post().json()
        finally:
            app.dependency_overrides.clear()

        assert (first["repeat"], repeat["repeat"]) == (False, True)
        assert first["books"][0]["ownership"] == "not_owned"
        assert repeat["books"][0]["ownership"] == "owned_other_edition"
        assert repeat["books"][0]["in_library"] is True
        (item,) = records.only()["enrichment"]
        assert "ownership" not in item and "already_in_library" not in item
