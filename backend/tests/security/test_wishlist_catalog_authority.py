"""Shared-catalog authority (#602): ``books`` / ``editions`` are written only
from server-produced provider data.

``POST /wishlist`` creates catalog rows only for a book whose
``catalog_token`` (signed by ``POST /scan`` or ``GET /books/search`` for this
user) matches every catalog field it sends. A forged title, description,
cover or edition with no token, a bad token, another user's token or a
tampered signed book creates nothing and is refused with 422
``catalog_token_invalid``; an existing book can still be linked without a
token; an existing edition is never overwritten.

Database-free (the CI security job has no Postgres): the session is a small
in-memory fake that records every ``add``. The same flows run on real
Postgres in ``tests/integration/test_wishlist_catalog_token_db.py``.

Run on its own with:  pytest -m security tests/security/test_wishlist_catalog_authority.py
"""

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai.providers.base import ProviderResult
from app.core import catalog_token
from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

pytestmark = pytest.mark.security

ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000602a1"), email="a@x.io")
BOB = User(id=uuid.UUID("00000000-0000-0000-0000-0000000602b2"), email="b@x.io")
NOW = datetime.now(UTC)


class FakeCatalogSession:
    """Just enough of AsyncSession for WishlistService: lookups by one
    column, ``add`` recorded, ids and timestamps filled on flush."""

    def __init__(self, books=(), editions=()):
        self.books = list(books)
        self.editions = list(editions)
        self.added: list = []

    def _rows(self, entity):
        return {Book: self.books, Edition: self.editions}.get(entity, [])

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        clause = stmt.whereclause
        match = None
        if entity in (Book, Edition) and hasattr(clause, "left"):
            column, value = clause.left.key, clause.right.value
            match = next(
                (r for r in self._rows(entity) if getattr(r, column) == value), None
            )
        result = MagicMock()
        result.scalar_one_or_none.return_value = match
        return result

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, Book):
            self.books.append(obj)
        elif isinstance(obj, Edition):
            self.editions.append(obj)

    async def flush(self):
        for obj in self.added:
            if obj.id is None:
                obj.id = uuid.uuid4()
            for attr in ("created_at", "updated_at"):
                if hasattr(obj, attr) and getattr(obj, attr) is None:
                    setattr(obj, attr, NOW)
            if isinstance(obj, Book) and obj.language is None:
                obj.language = "en"  # the column's server default

    async def commit(self):
        await self.flush()

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, UserBook):
            obj.book = next(b for b in self.books if b.id == obj.book_id)
            obj.edition = next(
                (e for e in self.editions if e.id == obj.edition_id), None
            )

    def catalog_writes(self):
        return [o for o in self.added if isinstance(o, Book | Edition)]


def _book(**overrides) -> EnrichedBook:
    data = {
        "open_library_work_id": f"OL{uuid.uuid4().hex[:8]}W",
        "google_books_id": None,
        "title": "Dune",
        "author": "Frank Herbert",
        "description": "Spice.",
        "cover_url": None,
        "subjects": ["Science fiction"],
        "confidence": 0.9,
        "editions": [EditionPreview(isbn_13="9780441013593", publisher="Ace")],
    }
    data.update(overrides)
    return EnrichedBook(**data)


def _body(book: EnrichedBook, **overrides) -> dict:
    """The JSON the app posts back to /wishlist."""
    data = book.model_dump(
        mode="json",
        exclude={"book_id", "confidence", "already_in_library", "in_library"},
    )
    data.update(overrides)
    return data


@pytest.fixture
def wishlist():
    """POST /wishlist as a user against a FakeCatalogSession."""
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    state = {"user": ALICE, "db": FakeCatalogSession()}

    async def _db():
        yield state["db"]

    def post(body, *, user=ALICE, db=None):
        app.dependency_overrides[get_current_user] = lambda: state["user"]
        app.dependency_overrides[get_db] = _db
        state["user"] = user
        if db is not None:
            state["db"] = db
        return TestClient(app).post("/wishlist", json=body), state["db"]

    yield post
    app.dependency_overrides.clear()


@pytest.fixture
def public_dns():
    """cover_url is validated by resolving it; the sandbox has no DNS."""
    with patch(
        "app.core.url_validator.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("93.184.216.34", 0))],
    ):
        yield


_FORGERIES = {
    "title": {"title": "Totally Real Book"},
    "description": {"description": "<a href=https://spam.example>buy</a>"},
    "cover": {"cover_url": "https://evil.example/cover.jpg"},
    "edition": {
        "editions": [
            {"isbn_13": "9780000000002", "publisher": "Forged", "page_count": 1}
        ]
    },
}


def _token_for(kind: str, book: EnrichedBook) -> str | None:
    if kind == "none":
        return None
    if kind == "garbage":
        return "not-a-token"
    if kind == "other_user":
        return catalog_token.sign(book, BOB.id)
    # "signed": the real token for the unforged book
    return catalog_token.sign(book, ALICE.id)


class TestForgedDataCreatesNothing:
    @pytest.mark.parametrize("forgery", sorted(_FORGERIES))
    @pytest.mark.parametrize("token", ["none", "garbage", "other_user", "signed"])
    def test_refused_with_422_and_no_catalog_rows(
        self, wishlist, public_dns, forgery, token
    ):
        book = _book()
        body = _body(book, **_FORGERIES[forgery])
        body["catalog_token"] = _token_for(token, book)
        resp, db = wishlist(body)
        assert resp.status_code == 422
        assert resp.json() == {"detail": "catalog_token_invalid"}
        assert db.catalog_writes() == []
        assert db.added == []

    def test_expired_token_is_refused(self, wishlist):
        book = _book()
        long_ago = datetime(2020, 1, 1, tzinfo=UTC)
        body = _body(book, catalog_token=catalog_token.sign(book, ALICE.id, long_ago))
        resp, db = wishlist(body)
        assert resp.status_code == 422
        assert db.catalog_writes() == []

    def test_unsigned_body_cannot_smuggle_ids(self, wishlist):
        """Server authority: book_id / user_id in the body change nothing."""
        body = _body(_book())
        body.update(
            {"book_id": str(uuid.uuid4()), "user_id": str(BOB.id), "trusted": True}
        )
        resp, db = wishlist(body)
        assert resp.status_code == 422
        assert db.added == []


class TestSignedData:
    def test_valid_token_creates_the_book_and_edition(self, wishlist):
        (book,) = catalog_token.sign_books([_book()], ALICE.id)
        resp, db = wishlist(_body(book))
        assert resp.status_code == 201, resp.text
        created_book, created_edition = db.catalog_writes()
        assert created_book.title == "Dune"
        assert created_book.open_library_work_id == book.open_library_work_id
        assert created_edition.isbn_13 == "9780441013593"
        assert resp.json()["book"]["title"] == "Dune"


class TestExistingCatalogRows:
    def _existing(self):
        book = Book(
            id=uuid.uuid4(),
            open_library_work_id="OL45804W",
            title="Dune",
            author="Frank Herbert",
            description="Original.",
            cover_url=None,
            subjects=["Science fiction"],
            language="en",
            created_at=NOW,
            updated_at=NOW,
        )
        edition = Edition(
            id=uuid.uuid4(),
            book_id=book.id,
            isbn_13="9780441013593",
            publisher="Ace",
            page_count=535,
            format="paperback",
            created_at=NOW,
        )
        return book, edition

    def test_existing_book_is_linked_without_a_token(self, wishlist):
        book, edition = self._existing()
        db = FakeCatalogSession([book], [edition])
        body = _body(
            _book(open_library_work_id="OL45804W"),
            title="Forged Title",
            description="Forged.",
        )
        resp, db = wishlist(body, db=db)
        assert resp.status_code == 201, resp.text
        assert db.catalog_writes() == []
        assert (book.title, book.description) == ("Dune", "Original.")
        (user_book,) = db.added
        assert (user_book.book_id, user_book.edition_id) == (book.id, edition.id)
        assert user_book.user_id == ALICE.id

    @pytest.mark.parametrize("signed", [False, True])
    def test_existing_edition_is_never_overwritten(self, wishlist, signed):
        book, edition = self._existing()
        db = FakeCatalogSession([book], [edition])
        sent = _book(
            open_library_work_id="OL45804W",
            editions=[
                EditionPreview(
                    isbn_13="9780441013593", publisher="Forged", page_count=1
                )
            ],
        )
        if signed:
            catalog_token.sign_books([sent], ALICE.id)
        resp, db = wishlist(_body(sent), db=db)
        assert resp.status_code == 201, resp.text
        assert db.catalog_writes() == []
        assert (edition.publisher, edition.page_count) == ("Ace", 535)
        assert (book.title, book.description) == ("Dune", "Original.")

    def test_unsigned_edition_of_another_book_is_not_linked(self, wishlist):
        book, _ = self._existing()
        other_book_edition = Edition(
            id=uuid.uuid4(), book_id=uuid.uuid4(), isbn_13="9780000000002"
        )
        db = FakeCatalogSession([book], [other_book_edition])
        body = _body(
            _book(
                open_library_work_id="OL45804W",
                editions=[EditionPreview(isbn_13="9780000000002")],
            )
        )
        resp, db = wishlist(body, db=db)
        assert resp.status_code == 201
        (user_book,) = db.added
        assert user_book.edition_id is None


# ─── Tokens on /scan and /books/search ────────────────────────────────────────

DUNE = BookCandidate(
    title="Dune", author="Frank Herbert", confidence=0.9, isbn_13="9780441013593"
)


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (6, 0, 2)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def scan_env():
    store = FakeQuotaStore()
    records = FakeScanRecords()
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    current = {"user": ALICE}

    async def _fake_db():
        yield AsyncMock()

    with (
        store.installed(),
        records.installed(),
        patch("app.ai.gateway.OpenAIVisionProvider") as cls,
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
        patch("app.api.scan.ledger.record", AsyncMock()),
    ):

        async def _enrich(candidates, limit):
            enrich.return_value.last_complete = True
            return [_book(open_library_work_id="OL45804W") for _ in candidates]

        cls.return_value.identify = AsyncMock(return_value=ProviderResult([DUNE]))
        enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
        dedup.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)

        def post(user):
            app.dependency_overrides[get_current_user] = lambda: current["user"]
            app.dependency_overrides[get_db] = _fake_db
            current["user"] = user
            return TestClient(app).post(
                "/scan", files={"file": ("s.jpg", io.BytesIO(_jpeg()), "image/jpeg")}
            )

        yield post, cls
    app.dependency_overrides.clear()


class TestResultsAreSignedPerUser:
    def test_scan_and_repeat_carry_tokens_for_this_user_only(self, scan_env):
        post, provider = scan_env
        first = post(ALICE).json()
        repeat = post(ALICE).json()
        assert (first["repeat"], repeat["repeat"]) == (False, True)
        assert provider.return_value.identify.await_count == 1
        for payload in (first, repeat):
            (book,) = payload["books"]
            req = EnrichedBook(**book)
            assert catalog_token.verify(book["catalog_token"], req, ALICE.id)
            assert not catalog_token.verify(book["catalog_token"], req, BOB.id)
        assert repeat["books"] == first["books"]

    def test_search_results_carry_tokens_for_this_user_only(self):
        from app.auth.dependencies import get_current_user

        app.dependency_overrides[get_current_user] = lambda: BOB
        volume = {"volumeInfo": {"title": "Dune", "authors": ["Frank Herbert"]}}
        try:
            with (
                patch(
                    "app.api.books._gb.search_query",
                    AsyncMock(return_value=[volume]),
                ),
                patch(
                    "app.api.books._enrichment.enrich",
                    AsyncMock(return_value=[_book()]),
                ),
            ):
                resp = TestClient(app).get("/books/search?q=dune")
        finally:
            app.dependency_overrides.clear()
        (book,) = resp.json()
        req = EnrichedBook(**book)
        assert catalog_token.verify(book["catalog_token"], req, BOB.id)
        assert not catalog_token.verify(book["catalog_token"], req, ALICE.id)

    def test_scan_result_round_trips_into_the_catalog(self, scan_env, wishlist):
        post, _ = scan_env
        (book,) = post(ALICE).json()["books"]
        body = {
            k: v
            for k, v in book.items()
            if k not in {"confidence", "already_in_library", "in_library"}
        }
        resp, db = wishlist(body)
        assert resp.status_code == 201, resp.text
        assert [type(o) for o in db.catalog_writes()] == [Book, Edition]
        # The same result, posted by Bob, is not his to create.
        resp, db = wishlist(body, user=BOB, db=FakeCatalogSession())
        assert resp.status_code == 422
        assert db.added == []
