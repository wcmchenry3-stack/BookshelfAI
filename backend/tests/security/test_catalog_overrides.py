"""User overrides (#682) never reach the shared catalog.

``PATCH /user-books/{id}`` takes ``title_override``, ``author_override`` and
``cover_url_override`` and writes them to the caller's own ``user_books``
row only:

- Whatever else the body carries (a ``book`` object, catalog field names,
  ``book_id``, ``source``, ``merged_into_id``) changes no ``books`` /
  ``editions`` row, adds none, and does not re-point the entry.
- Override text is bounded and free of control characters; SQL / XSS
  strings are stored as plain text in the user's row (bound parameters).
- ``cover_url_override`` goes through the SSRF-safe URL check: https only,
  a resolvable host, no private / loopback / link-local / metadata address.
- The endpoint still needs a valid session and keeps the write rate limit.

Database-free (the CI security job has no Postgres). The same flows run on
real Postgres in ``tests/integration/test_catalog_user_split_db.py``.

Run on its own with:  pytest -m security tests/security/test_catalog_overrides.py
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from limits import parse

from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.book import Book
from app.models.edition import Edition
from app.models.user import User
from app.models.user_book import UserBook
from tests.security.test_injection import SQL_INJECTION_PAYLOADS, XSS_PAYLOADS

pytestmark = pytest.mark.security

NOW = datetime.now(UTC)
ALICE = User(id=uuid.UUID("00000000-0000-0000-0000-0000000682a1"), email="a@x.io")
CATALOG = {
    "title": "Dune",
    "author": "Frank Herbert",
    "cover_url": "https://books.example/dune.jpg",
    "description": "Spice.",
}


def _catalog_book() -> Book:
    return Book(
        id=uuid.uuid4(),
        open_library_work_id="OL45804W",
        language="en",
        subjects=[],
        editions=[],
        created_at=NOW,
        updated_at=NOW,
        **CATALOG,
    )


def _snapshot(book: Book) -> dict:
    return {
        name: getattr(book, name)
        for name in (
            *CATALOG,
            "id",
            "open_library_work_id",
            "google_books_id",
            "source",
            "fetched_at",
            "merged_into_id",
        )
    }


@pytest.fixture
def patch_entry():
    """PATCH /user-books/{id} as Alice, against a fake session holding one
    catalog entry; records everything added."""
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    book = _catalog_book()
    entry = UserBook(
        id=uuid.uuid4(),
        user_id=ALICE.id,
        book_id=book.id,
        book=book,
        status="wishlisted",
        created_at=NOW,
        updated_at=NOW,
    )
    added: list = []

    async def _db():
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = entry
        result.scalar_one.return_value = entry
        db.execute = AsyncMock(return_value=result)
        db.add = MagicMock(side_effect=added.append)
        yield db

    app.dependency_overrides[get_current_user] = lambda: ALICE
    app.dependency_overrides[get_db] = _db

    def call(body):
        return TestClient(app).patch(f"/user-books/{entry.id}", json=body)

    yield call, entry, book, added
    app.dependency_overrides.clear()


class TestCatalogIsNeverWritten:
    def test_overrides_change_only_the_users_row(self, patch_entry):
        call, entry, book, added = patch_entry
        before = _snapshot(book)
        resp = call(
            {
                "title_override": "Mine",
                "author_override": "Me",
                "cover_url_override": None,
            }
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["book"]["title"] == "Mine"
        assert _snapshot(book) == before
        assert (entry.title_override, entry.author_override) == ("Mine", "Me")
        assert not [o for o in added if isinstance(o, Book | Edition)]

    def test_smuggled_catalog_fields_are_ignored(self, patch_entry):
        call, entry, book, added = patch_entry
        before = _snapshot(book)
        other = uuid.uuid4()
        resp = call(
            {
                "title": "Forged",
                "author": "Forged",
                "cover_url": "https://evil.example/c.jpg",
                "description": "Forged",
                "book": {"id": str(other), "title": "Forged"},
                "book_id": str(other),
                "source": "google_books",
                "merged_into_id": str(other),
                "title_override": "Mine",
            }
        )
        assert resp.status_code == 200, resp.text
        assert _snapshot(book) == before
        assert entry.book_id == book.id
        assert entry.book is book
        assert not [o for o in added if isinstance(o, Book | Edition)]
        assert resp.json()["book"]["id"] == str(book.id)


class TestOverrideTextValidation:
    @pytest.mark.parametrize("payload", SQL_INJECTION_PAYLOADS + XSS_PAYLOADS)
    def test_injection_strings_stay_plain_text_in_the_users_row(
        self, patch_entry, payload
    ):
        call, entry, book, _ = patch_entry
        resp = call({"title_override": payload, "author_override": payload})
        if payload.strip() and len(payload) <= 500:
            assert resp.status_code == 200, resp.text
            assert entry.title_override == payload.strip()
        else:
            assert resp.status_code == 422
        assert book.title == "Dune"

    @pytest.mark.parametrize(
        "body",
        [
            {"title_override": "x" * 501},
            {"author_override": "x" * 501},
            {"title_override": ""},
            {"author_override": "  "},
            {"title_override": "a\x00b"},
            {"author_override": "a\x1bb"},
            {"title_override": "a\u0085b"},
            {"title_override": 5},
            # Unicode format characters (Cf): bidi override, zero-width.
            {"title_override": "a\u202eb"},
            {"author_override": "a\u200bb"},
            {"title_override": "\ufeffDune"},
            {"author_override": "a\u2066b"},
        ],
    )
    def test_invalid_text_is_refused_with_nothing_written(self, patch_entry, body):
        call, entry, _, _ = patch_entry
        resp = call(body)
        assert resp.status_code == 422
        assert entry.title_override is None and entry.author_override is None

    @pytest.mark.parametrize("field", ["title_override", "author_override"])
    def test_length_limit_applies_after_trimming(self, patch_entry, field):
        call, entry, _, _ = patch_entry
        resp = call({field: " " * 10 + "x" * 500 + "\n"})
        assert resp.status_code == 200, resp.text
        assert getattr(entry, field) == "x" * 500


class TestCoverUrlSsrf:
    """The override is never fetched by the server (only the owner's app
    shows it), so its host is not resolved: a DNS lookup would block the
    event loop (#737 review). Hosts that name a private address are still
    refused."""

    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "[::1]",
            "[fd00::1]",
            "[::ffff:10.0.0.1]",
            "localhost",
            "covers.localhost",
        ],
    )
    def test_private_host_is_refused_without_resolving(self, patch_entry, host):
        call, entry, _, _ = patch_entry
        with patch("app.core.url_validator.socket.getaddrinfo") as resolve:
            resp = call({"cover_url_override": f"https://{host}/c.jpg"})
        assert resp.status_code == 422
        resolve.assert_not_called()
        assert entry.cover_url_override is None

    @pytest.mark.parametrize(
        "url",
        [
            "http://covers.example/c.jpg",
            "ftp://covers.example/c.jpg",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https:///nohost",
        ],
    )
    def test_unsafe_urls_are_refused(self, patch_entry, url):
        call, entry, _, _ = patch_entry
        resp = call({"cover_url_override": url})
        assert resp.status_code == 422
        assert entry.cover_url_override is None

    def test_host_name_is_never_resolved(self, patch_entry):
        call, entry, _, _ = patch_entry
        with patch(
            "app.core.url_validator.socket.getaddrinfo", side_effect=OSError("nx")
        ) as resolve:
            resp = call({"cover_url_override": "https://nx.example/c.jpg"})
        assert resp.status_code == 200, resp.text
        resolve.assert_not_called()
        assert entry.cover_url_override == "https://nx.example/c.jpg"

    def test_oversized_url_is_refused_without_resolving(self, patch_entry):
        call, entry, _, _ = patch_entry
        with patch("app.core.url_validator.socket.getaddrinfo") as resolve:
            resp = call({"cover_url_override": "https://x.example/" + "a" * 2048})
        assert resp.status_code == 422
        resolve.assert_not_called()
        assert entry.cover_url_override is None

    def test_public_https_cover_is_stored_on_the_users_row_only(self, patch_entry):
        call, entry, book, _ = patch_entry
        resp = call({"cover_url_override": "https://covers.example/c.jpg"})
        assert resp.status_code == 200, resp.text
        assert entry.cover_url_override == "https://covers.example/c.jpg"
        assert book.cover_url == CATALOG["cover_url"]
        assert resp.json()["book"]["cover_url"] == "https://covers.example/c.jpg"


class TestCoverUrlCharacters:
    """A cover URL with control characters, whitespace inside it or format
    characters is refused (422) before it is resolved or stored; a NUL used
    to reach Postgres and fail with a 500."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://covers.example/c.jpg\x00",
            "https://covers.example/c\x00.jpg",
            "https://covers.example/c\n.jpg",
            "https://covers.example/c\t.jpg",
            "https://covers.example/c .jpg",
            "https://covers.example/c\u202e.jpg",
            "https://covers.example/c\u200b.jpg",
            "https://covers.example/c\x85.jpg",
        ],
    )
    def test_refused_without_resolving(self, patch_entry, url):
        call, entry, _, _ = patch_entry
        with patch("app.core.url_validator.socket.getaddrinfo") as resolve:
            resp = call({"cover_url_override": url})
        assert resp.status_code == 422, resp.text
        resolve.assert_not_called()
        assert entry.cover_url_override is None

    @pytest.mark.parametrize("padded", [" {}", "{}\n", "\t{} "])
    def test_surrounding_whitespace_is_stripped(self, patch_entry, padded):
        call, entry, _, _ = patch_entry
        url = "https://covers.example/c.jpg"
        resp = call({"cover_url_override": padded.format(url)})
        assert resp.status_code == 200, resp.text
        assert entry.cover_url_override == url


class TestAuthAndRateLimit:
    def test_requires_a_session(self):
        app.dependency_overrides.clear()
        resp = TestClient(app).patch(
            f"/user-books/{uuid.uuid4()}", json={"title_override": "X"}
        )
        assert resp.status_code == 401

    def test_malformed_bearer_is_refused(self):
        app.dependency_overrides.clear()
        resp = TestClient(app).patch(
            f"/user-books/{uuid.uuid4()}",
            json={"title_override": "X"},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert resp.status_code == 401

    def test_update_keeps_the_write_rate_limit(self):
        route_limits = limiter._route_limits["app.api.user_books.update_user_book"]
        assert [str(rl.limit) for rl in route_limits] == [
            str(parse(settings.rate_limit_writes))
        ]

    def test_list_keeps_the_read_rate_limit(self):
        route_limits = limiter._route_limits["app.api.user_books.list_user_books"]
        assert [str(rl.limit) for rl in route_limits] == [
            str(parse(settings.rate_limit_reads))
        ]
