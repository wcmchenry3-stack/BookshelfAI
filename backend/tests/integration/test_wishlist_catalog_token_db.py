"""Catalog tokens end to end on real Postgres (#602).

A result from ``GET /books/search``, a new ``POST /scan`` and a repeat served
from the scan record, posted back to ``POST /wishlist`` with its
``catalog_token``, creates the ``books`` / ``editions`` rows. The same data
without a valid token creates no rows (422 ``catalog_token_invalid``),
an existing book is still linked, and an existing edition is never
overwritten.

The providers are faked: the vision provider at the gateway boundary, Open
Library and Google Books at their service methods (the real
EnrichmentService runs).

Needs DATABASE_URL pointing at a Postgres database.
"""

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import scan_records
from app.ai.providers.base import ProviderResult
from app.api import books as books_api
from app.api import scan as scan_api
from app.api import user_books as user_books_api
from app.core import catalog_token
from app.core.config import settings
from app.core.database import Base
from app.models.ai_usage import AIUsage
from app.models.book import Book
from app.models.edition import Edition
from app.models.scan_record import ScanRecord
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.user_book import WishlistRequest
from app.services.book_identifier import BookCandidate
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

pytestmark = pytest.mark.integration


@pytest.fixture
async def env():
    """Sessions, users, a unique work / volume / ISBN per test, and the
    faked providers; every row this test made is deleted afterwards."""
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    tag = uuid.uuid4().hex[:10]
    isbn = "979" + str(uuid.uuid4().int)[:10]
    ids = {
        "work": f"OL{tag}W",
        "volume": f"gb{tag}",
        "isbn": isbn,
    }

    async def make_user() -> User:
        uid = uuid.uuid4()
        async with factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        users.append(uid)
        return uid

    volume = {
        "id": ids["volume"],
        "volumeInfo": {
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "description": "Spice.",
            "publisher": "Ace",
            "publishedDate": "1990-09-01",
            "pageCount": 535,
            "imageLinks": {"thumbnail": "http://books.google.com/dune.jpg"},
            "industryIdentifiers": [{"type": "ISBN_13", "identifier": isbn}],
        },
    }
    doc = {
        "key": f"/works/{ids['work']}",
        "title": "Dune",
        "author_name": ["Frank Herbert"],
        "subject": ["Science fiction"],
    }
    candidate = BookCandidate(
        title="Dune", author="Frank Herbert", confidence=0.9, isbn_13=isbn
    )
    provider = MagicMock()
    provider.return_value.identify = AsyncMock(return_value=ProviderResult([candidate]))
    with (
        patch.object(OpenLibraryService, "search_by_isbn", AsyncMock(return_value=doc)),
        patch.object(OpenLibraryService, "search", AsyncMock(return_value=doc)),
        patch.object(
            GoogleBooksService, "search_by_isbn", AsyncMock(return_value=volume)
        ),
        patch.object(GoogleBooksService, "search", AsyncMock(return_value=volume)),
        patch.object(
            GoogleBooksService, "search_query", AsyncMock(return_value=[volume])
        ),
        patch("app.ai.gateway.OpenAIVisionProvider", provider),
        patch.object(settings, "turnstile_required", False),
        # cover_url is validated by resolving it; no DNS in CI sandboxes.
        patch(
            "app.core.url_validator.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("142.250.0.1", 0))],
        ),
    ):
        yield factory, make_user, ids, provider

    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        book_ids = select(Book.id).where(Book.open_library_work_id == ids["work"])
        await db.execute(delete(Edition).where(Edition.book_id.in_(book_ids)))
        await db.execute(delete(Edition).where(Edition.isbn_13 == ids["isbn"]))
        await db.execute(delete(Book).where(Book.open_library_work_id == ids["work"]))
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(users)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (24, 16), (60, 2, 2)).save(buf, format="JPEG")
    return buf.getvalue()


async def _search(factory, user_id):
    async with factory() as db:
        user = await db.get(User, user_id)
    return await books_api.search_books.__wrapped__(
        request=MagicMock(), q="dune", current_user=user
    )


async def _scan(factory, user_id, image: bytes):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=StarletteUploadFile(
                file=io.BytesIO(image),
                filename="scan.jpg",
                headers=Headers({"content-type": "image/jpeg"}),
            ),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=False,
        )


def _posted_back(book, **overrides) -> WishlistRequest:
    """The app's JSON round trip: EnrichedBook -> JSON -> WishlistRequest."""
    data = book.model_dump(mode="json")
    data.update(overrides)
    return WishlistRequest.model_validate(data)


async def _wishlist(factory, user_id, req: WishlistRequest):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await user_books_api.add_to_wishlist.__wrapped__(
            request=MagicMock(), req=req, current_user=user, db=db
        )


async def _catalog(factory, ids) -> tuple[list[Book], list[Edition]]:
    async with factory() as db:
        books = list(
            (
                await db.execute(
                    select(Book).where(Book.open_library_work_id == ids["work"])
                )
            ).scalars()
        )
        editions = list(
            (
                await db.execute(select(Edition).where(Edition.isbn_13 == ids["isbn"]))
            ).scalars()
        )
    return books, editions


async def _count(factory, model) -> int:
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


class TestSignedResultsCreateCatalogRows:
    async def test_search_result(self, env):
        factory, make_user, ids, _ = env
        user_id = await make_user()
        (book,) = await _search(factory, user_id)
        assert book.catalog_token
        ub = await _wishlist(factory, user_id, _posted_back(book))
        (row,), (edition,) = await _catalog(factory, ids)
        assert (row.title, row.google_books_id) == ("Dune", ids["volume"])
        assert row.cover_url == "https://books.google.com/dune.jpg"
        assert (edition.publisher, edition.page_count) == ("Ace", 535)
        assert (ub.book_id, ub.edition_id) == (row.id, edition.id)

    async def test_scan_result(self, env):
        factory, make_user, ids, _ = env
        user_id = await make_user()
        resp = await _scan(factory, user_id, _jpeg())
        assert resp.repeat is False
        (book,) = resp.books
        assert book.catalog_token
        await _wishlist(factory, user_id, _posted_back(book))
        (row,), (edition,) = await _catalog(factory, ids)
        assert row.title == "Dune" and edition.isbn_13 == ids["isbn"]

    async def test_repeat_served_from_the_scan_record(self, env):
        factory, make_user, ids, provider = env
        user_id = await make_user()
        image = _jpeg()
        first = await _scan(factory, user_id, image)
        repeat = await _scan(factory, user_id, image)
        assert (first.repeat, repeat.repeat) == (False, True)
        assert provider.return_value.identify.await_count == 1
        (book,) = repeat.books
        assert book.catalog_token
        assert book.catalog_token == first.books[0].catalog_token
        await _wishlist(factory, user_id, _posted_back(book))
        (row,), (edition,) = await _catalog(factory, ids)
        assert row.title == "Dune" and edition.isbn_13 == ids["isbn"]


class TestUnsignedDataCreatesNothing:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"catalog_token": None},
            {"catalog_token": "forged.token"},
            {"title": "Forged Title"},
            {"description": "Forged."},
            {"cover_url": "https://evil.example/c.jpg"},
            {"editions": [{"isbn_13": "9780000000002", "publisher": "Forged"}]},
        ],
        ids=["no_token", "bad_token", "title", "description", "cover", "edition"],
    )
    async def test_422_and_no_rows(self, env, overrides):
        factory, make_user, ids, _ = env
        user_id = await make_user()
        (book,) = await _search(factory, user_id)
        before = (await _count(factory, Book), await _count(factory, Edition))
        with pytest.raises(HTTPException) as exc:
            await _wishlist(factory, user_id, _posted_back(book, **overrides))
        assert (exc.value.status_code, exc.value.detail) == (
            422,
            "catalog_token_invalid",
        )
        assert (await _count(factory, Book), await _count(factory, Edition)) == before
        assert await _catalog(factory, ids) == ([], [])

    async def test_another_users_result(self, env):
        factory, make_user, ids, _ = env
        alice, bob = await make_user(), await make_user()
        (book,) = await _search(factory, alice)
        with pytest.raises(HTTPException) as exc:
            await _wishlist(factory, bob, _posted_back(book))
        assert exc.value.status_code == 422
        assert await _catalog(factory, ids) == ([], [])


class TestExistingRows:
    async def test_existing_book_linked_without_a_token_and_never_changed(self, env):
        factory, make_user, ids, _ = env
        alice, bob = await make_user(), await make_user()
        (book,) = await _search(factory, alice)
        await _wishlist(factory, alice, _posted_back(book))
        # Bob sends the same work with forged fields and no token.
        forged = _posted_back(
            book,
            catalog_token=None,
            title="Forged Title",
            description="Forged.",
            editions=[{"isbn_13": ids["isbn"], "publisher": "Forged"}],
        )
        ub = await _wishlist(factory, bob, forged)
        (row,), (edition,) = await _catalog(factory, ids)
        assert (row.title, row.description) == ("Dune", "Spice.")
        assert edition.publisher == "Ace"
        assert (ub.book_id, ub.edition_id) == (row.id, edition.id)

    async def test_existing_edition_not_overwritten_by_a_signed_result(self, env):
        factory, make_user, ids, _ = env
        user_id = await make_user()
        async with factory() as db:
            db.add(
                Book(
                    open_library_work_id=f"{ids['work']}x",
                    title="Other printing",
                    author="Frank Herbert",
                )
            )
            await db.flush()
            other = (
                await db.execute(
                    select(Book).where(Book.open_library_work_id == f"{ids['work']}x")
                )
            ).scalar_one()
            db.add(
                Edition(
                    book_id=other.id,
                    isbn_13=ids["isbn"],
                    publisher="First Writer",
                    format="paperback",
                )
            )
            await db.commit()
        try:
            (book,) = await _search(factory, user_id)
            await _wishlist(factory, user_id, _posted_back(book))
            (row,), (edition,) = await _catalog(factory, ids)
            assert edition.publisher == "First Writer"
            assert edition.book_id == other.id
            # Never linked to another book's edition (#602 review, finding 2).
            async with factory() as db:
                ub = (
                    await db.execute(
                        select(UserBook).where(UserBook.user_id == user_id)
                    )
                ).scalar_one()
            assert (ub.book_id, ub.edition_id) == (row.id, None)
        finally:
            async with factory() as db:
                await db.execute(delete(UserBook).where(UserBook.user_id == user_id))
                await db.execute(delete(Edition).where(Edition.book_id == other.id))
                await db.execute(delete(Book).where(Book.id == other.id))
                await db.commit()


class TestProviderFieldsOnly:
    """What a scan signs, and so what POST /wishlist stores, is the
    provider's record, never the photo's reading (#602 review, finding 1)."""

    DOCTORED = "Dune — FREE CRYPTO at evil.example"

    async def test_doctored_cover_stores_the_provider_title(self, env):
        factory, make_user, ids, provider = env
        candidate = BookCandidate(
            title=self.DOCTORED,
            author="Not Herbert",
            confidence=0.9,
            isbn_13=ids["isbn"],
        )
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([candidate])
        )
        user_id = await make_user()
        (book,) = (await _scan(factory, user_id, _jpeg())).books
        assert (book.title, book.author) == ("Dune", "Frank Herbert")
        await _wishlist(factory, user_id, _posted_back(book))
        (row,), _ = await _catalog(factory, ids)
        assert (row.title, row.author) == ("Dune", "Frank Herbert")

    async def test_repeat_of_a_record_saved_before_the_fix_is_re_enriched(self, env):
        factory, make_user, _ids, provider = env
        user_id = await make_user()
        image = _jpeg()
        await _scan(factory, user_id, image)
        # Rewrite the saved enrichment as an older record would hold it: the
        # model's title, no format marker.
        async with factory() as db:
            record = (
                await db.execute(
                    select(ScanRecord).where(ScanRecord.user_id == user_id)
                )
            ).scalar_one()
            (item,) = record.enrichment
            legacy = {
                k: v for k, v in item.items() if k != scan_records.BOOK_FORMAT_KEY
            }
            record.enrichment = [{**legacy, "title": self.DOCTORED}]
            await db.commit()
        repeat = await _scan(factory, user_id, image)
        assert repeat.repeat is True
        assert provider.return_value.identify.await_count == 1  # no AI call
        (book,) = repeat.books
        assert book.title == "Dune" and book.catalog_token
        async with factory() as db:
            record = (
                await db.execute(
                    select(ScanRecord).where(ScanRecord.user_id == user_id)
                )
            ).scalar_one()
        (item,) = record.enrichment
        assert item["title"] == "Dune"
        assert item[scan_records.BOOK_FORMAT_KEY] == scan_records.BOOK_FORMAT_VERSION
        async with factory() as db:
            await db.execute(delete(ScanRecord).where(ScanRecord.user_id == user_id))
            await db.commit()


class TestNoProviderIdCreatesNothing:
    async def test_replayed_token_for_a_book_without_ids(self, env):
        """A token a server once signed for a book with no provider ID
        creates no row, however often it is replayed (finding 3)."""
        factory, make_user, _ids, _ = env
        user_id = await make_user()
        req = WishlistRequest(title=f"Zine {uuid.uuid4().hex}", author="Me")
        req.catalog_token = catalog_token.sign(req, user_id)
        before = await _count(factory, Book)
        for _ in range(3):
            with pytest.raises(HTTPException) as exc:
                await _wishlist(factory, user_id, req)
            assert (exc.value.status_code, exc.value.detail) == (
                422,
                "book_not_in_catalog",
            )
        assert await _count(factory, Book) == before


def _hiding(db, **hidden: int):
    """Make ``db``'s first N lookups of each named entity find nothing, as
    if another request inserted the row just after them (a race)."""
    real = db.execute

    async def execute(stmt, *args, **kwargs):
        descriptions = getattr(stmt, "column_descriptions", None)
        entity = descriptions[0].get("entity") if descriptions else None
        name = getattr(entity, "__name__", None)
        if hidden.get(name, 0) > 0:
            hidden[name] -= 1
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result
        return await real(stmt, *args, **kwargs)

    db.execute = execute
    return db


class TestConcurrentFirstAdds:
    """Two first-time adds of the same work: the loser of the unique
    constraint links the winner's row instead of failing (finding 3)."""

    async def _add_racing(self, factory, user_id, req, **hidden):
        async with factory() as db:
            user = await db.get(User, user_id)
            _hiding(db, **hidden)
            return await user_books_api.add_to_wishlist.__wrapped__(
                request=MagicMock(), req=req, current_user=user, db=db
            )

    async def test_book_created_by_another_request_is_linked(self, env):
        factory, make_user, ids, _ = env
        alice, bob = await make_user(), await make_user()
        (a_book,) = await _search(factory, alice)
        (b_book,) = await _search(factory, bob)
        await _wishlist(factory, alice, _posted_back(a_book))
        # Bob's work-ID and volume-ID lookups ran before Alice's insert.
        ub = await self._add_racing(factory, bob, _posted_back(b_book), Book=2)
        (row,), (edition,) = await _catalog(factory, ids)
        assert (ub.book_id, ub.edition_id) == (row.id, edition.id)

    async def test_edition_created_by_another_request_is_linked(self, env):
        factory, make_user, ids, _ = env
        alice, bob = await make_user(), await make_user()
        (a_book,) = await _search(factory, alice)
        (b_book,) = await _search(factory, bob)
        await _wishlist(factory, alice, _posted_back(a_book))
        ub = await self._add_racing(factory, bob, _posted_back(b_book), Edition=1)
        (row,), (edition,) = await _catalog(factory, ids)
        assert (ub.book_id, ub.edition_id) == (row.id, edition.id)
