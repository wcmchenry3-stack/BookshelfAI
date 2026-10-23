"""Edition-level library matching end to end on real Postgres (#587).

``ownership`` on ``POST /scan`` results (new photos and repeats served from
the scan record), on ``GET /books/search`` results, and the 409 codes of
``POST /wishlist`` for a book the user already has, which writes nothing.

The providers are faked as in ``test_wishlist_catalog_token_db.py``: the
vision provider at the gateway boundary, Open Library and Google Books at
their service methods (the real EnrichmentService runs).

Needs DATABASE_URL pointing at a Postgres database.
"""

import io
import random
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai.providers.base import ProviderResult
from app.api import books as books_api
from app.api import scan as scan_api
from app.api import user_books as user_books_api
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
from app.services.enrichment import isbn_10_to_13, isbn_13_to_10
from app.services.google_books import GoogleBooksService
from app.services.open_library import OpenLibraryService

pytestmark = pytest.mark.integration


def _isbn_pair() -> tuple[str, str]:
    """A fresh, valid (ISBN-13, ISBN-10) pair for the same edition."""
    body = "".join(random.choice("0123456789") for _ in range(9))
    isbn_10 = isbn_13_to_10("978" + body + "0")
    return isbn_10_to_13(isbn_10), isbn_10


@pytest.fixture
async def env():
    """Sessions, users, a unique work / volume / ISBNs per test and the
    faked providers; ``shown`` sets which ISBNs the photo and the provider
    records carry. Every row this test made is deleted afterwards."""
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    users: list[uuid.UUID] = []
    tag = uuid.uuid4().hex[:10]
    isbn_13, isbn_10 = _isbn_pair()
    other_13, _ = _isbn_pair()
    ids = {
        "work": f"OL{tag}W",
        "volume": f"gb{tag}",
        "isbn_13": isbn_13,
        "isbn_10": isbn_10,
        "other_13": other_13,
    }

    async def make_user() -> uuid.UUID:
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
            "publisher": "Ace",
            "industryIdentifiers": [],
        },
    }
    doc = {
        "key": f"/works/{ids['work']}",
        "title": "Dune",
        "author_name": ["Frank Herbert"],
        "subject": ["Science fiction"],
    }
    provider = MagicMock()

    def shown(*, isbn_13=None, isbn_10=None, work=None):
        """What the photo / search shows and the provider records confirm."""
        idents = []
        if isbn_13:
            idents.append({"type": "ISBN_13", "identifier": isbn_13})
        if isbn_10:
            idents.append({"type": "ISBN_10", "identifier": isbn_10})
        volume["volumeInfo"]["industryIdentifiers"] = idents
        doc["key"] = f"/works/{work or ids['work']}"
        candidate = BookCandidate(
            title="Dune",
            author="Frank Herbert",
            confidence=0.9,
            isbn_13=isbn_13,
            isbn_10=isbn_10,
        )
        provider.return_value.identify = AsyncMock(
            return_value=ProviderResult([candidate])
        )

    shown(isbn_13=ids["isbn_13"])
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
    ):
        yield factory, make_user, ids, shown

    async with factory() as db:
        await db.execute(delete(UserBook).where(UserBook.user_id.in_(users)))
        ours = (Book.open_library_work_id.like(f"OL{tag}%")) | (
            Book.google_books_id == ids["volume"]
        )
        book_ids = select(Book.id).where(ours)
        await db.execute(delete(Edition).where(Edition.book_id.in_(book_ids)))
        await db.execute(delete(Book).where(ours))
        await db.execute(delete(ScanRecord).where(ScanRecord.user_id.in_(users)))
        await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(users)))
        await db.execute(delete(User).where(User.id.in_(users)))
        await db.commit()
    await engine.dispose()


def _jpeg() -> bytes:
    buf = io.BytesIO()
    color = tuple(random.randrange(256) for _ in range(3))
    Image.new("RGB", (24, 16), color).save(buf, format="JPEG")
    return buf.getvalue()


async def _scan(factory, user_id, image: bytes | None = None):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await scan_api.scan.__wrapped__(
            request=MagicMock(),
            file=StarletteUploadFile(
                file=io.BytesIO(image or _jpeg()),
                filename="scan.jpg",
                headers=Headers({"content-type": "image/jpeg"}),
            ),
            current_user=user,
            db=db,
            cf_turnstile_response=None,
            enhanced=False,
        )


async def _search(factory, user_id):
    async with factory() as db:
        user = await db.get(User, user_id)
        return await books_api.search_books.__wrapped__(
            request=MagicMock(), q="dune", current_user=user, db=db
        )


async def _wishlist(factory, user_id, book):
    req = WishlistRequest.model_validate(book.model_dump(mode="json"))
    async with factory() as db:
        user = await db.get(User, user_id)
        return await user_books_api.add_to_wishlist.__wrapped__(
            request=MagicMock(), req=req, current_user=user, db=db
        )


async def _refused(factory, user_id, book) -> str:
    before = await _counts(factory)
    with pytest.raises(HTTPException) as exc:
        await _wishlist(factory, user_id, book)
    assert exc.value.status_code == 409
    assert await _counts(factory) == before  # nothing written
    return exc.value.detail


async def _counts(factory) -> tuple[int, int, int]:
    async with factory() as db:
        return tuple(
            [
                (await db.execute(select(func.count()).select_from(m))).scalar_one()
                for m in (Book, Edition, UserBook)
            ]
        )


async def _own(
    factory, user_id, ids, *, isbn_13=None, isbn_10=None, work=None, volume=None
):
    """Put the work in the user's library, with an edition holding these
    ISBNs (none: an entry without an edition). With ``volume`` the Book is
    Google-Books-only (no work ID)."""
    async with factory() as db:
        book = Book(
            open_library_work_id=None if volume else (work or ids["work"]),
            google_books_id=volume,
            title="Dune",
            author="Frank Herbert",
        )
        db.add(book)
        await db.flush()
        edition = None
        if isbn_13 or isbn_10:
            edition = Edition(
                book_id=book.id, isbn_13=isbn_13, isbn_10=isbn_10, format="paperback"
            )
            db.add(edition)
            await db.flush()
        db.add(
            UserBook(
                user_id=user_id,
                book_id=book.id,
                edition_id=edition.id if edition else None,
                status="purchased",
            )
        )
        await db.commit()


class TestSearchLibraryCheckFailure:
    async def test_failed_check_still_answers_on_a_real_session(self, env):
        """The check's rollback expires the session's user; the search must
        not reload it afterwards (a 500 on a real AsyncSession)."""
        factory, make_user, _ids, _ = env
        alice = await make_user()
        with patch(
            "app.api.books.DeduplicationService.check",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            (found,) = await _search(factory, alice)
        assert found.ownership == "not_owned" and found.in_library is False
        assert found.catalog_token


class TestSameEdition:
    async def test_added_from_search_then_scanned_and_re_added(self, env):
        factory, make_user, _ids, _ = env
        alice = await make_user()
        (found,) = await _search(factory, alice)
        assert found.ownership == "not_owned" and found.in_library is False
        await _wishlist(factory, alice, found)
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.ownership == "owned_same_edition"
        assert scanned.already_in_library is True and scanned.in_library is True
        (again,) = await _search(factory, alice)
        assert again.ownership == "owned_same_edition"
        assert await _refused(factory, alice, again) == "owned_same_edition"

    async def test_isbn_10_result_against_owned_isbn_13(self, env):
        factory, make_user, ids, shown = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_13=ids["isbn_13"])
        shown(isbn_10=ids["isbn_10"])
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.editions[0].isbn_13 is None
        assert scanned.ownership == "owned_same_edition"
        assert await _refused(factory, alice, scanned) == "owned_same_edition"

    async def test_isbn_13_result_against_owned_isbn_10_only(self, env):
        factory, make_user, ids, _ = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_10=ids["isbn_10"])
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.ownership == "owned_same_edition"
        assert await _refused(factory, alice, scanned) == "owned_same_edition"

    async def test_same_isbn_under_another_work_creates_no_rows(self, env):
        """The edition the user has belongs to another catalog row of the
        title: a signed add would create a new Book, but is refused and
        nothing is written."""
        factory, make_user, ids, _ = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_13=ids["isbn_13"], work=f"{ids['work']}x")
        (found,) = await _search(factory, alice)
        assert found.open_library_work_id == ids["work"]
        assert found.ownership == "owned_same_edition"
        assert await _refused(factory, alice, found) == "owned_same_edition"
        async with factory() as db:
            assert (
                await db.execute(
                    select(Book).where(Book.open_library_work_id == ids["work"])
                )
            ).scalar_one_or_none() is None


class TestOtherEdition:
    async def test_scan_search_and_409(self, env):
        factory, make_user, ids, _ = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_13=ids["other_13"])
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.ownership == "owned_other_edition"
        assert scanned.in_library is True
        (found,) = await _search(factory, alice)
        assert found.ownership == "owned_other_edition"
        assert await _refused(factory, alice, found) == "owned_other_edition"


class TestEditionUnknown:
    async def test_owned_entry_without_an_edition(self, env):
        factory, make_user, ids, _ = env
        alice = await make_user()
        await _own(factory, alice, ids)
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.ownership == "owned_edition_unknown"
        assert await _refused(factory, alice, scanned) == "owned_edition_unknown"

    async def test_result_without_an_isbn(self, env):
        factory, make_user, ids, shown = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_13=ids["isbn_13"])
        shown()
        (scanned,) = (await _scan(factory, alice)).books
        assert scanned.editions[0].isbn_13 is None
        assert scanned.editions[0].isbn_10 is None
        assert scanned.ownership == "owned_edition_unknown"
        assert await _refused(factory, alice, scanned) == "owned_edition_unknown"


class TestOnlyTheUsersOwnLibrary:
    async def test_another_users_book_does_not_count(self, env):
        factory, make_user, ids, _ = env
        alice, bob = await make_user(), await make_user()
        await _own(factory, alice, ids, isbn_13=ids["isbn_13"])
        (scanned,) = (await _scan(factory, bob)).books
        assert scanned.ownership == "not_owned" and scanned.in_library is False
        (found,) = await _search(factory, bob)
        assert found.ownership == "not_owned"
        ub = await _wishlist(factory, bob, found)
        assert ub.edition is not None and ub.edition.isbn_13 == ids["isbn_13"]


class TestScanRepeat:
    async def test_repeat_checks_the_library_as_it_is_now(self, env):
        factory, make_user, _ids, _ = env
        alice = await make_user()
        image = _jpeg()
        first = await _scan(factory, alice, image)
        (book,) = first.books
        assert first.repeat is False and book.ownership == "not_owned"
        await _wishlist(factory, alice, book)
        repeat = await _scan(factory, alice, image)
        (again,) = repeat.books
        assert repeat.repeat is True
        assert again.ownership == "owned_same_edition"
        assert await _refused(factory, alice, again) == "owned_same_edition"
        async with factory() as db:
            record = (
                await db.execute(select(ScanRecord).where(ScanRecord.user_id == alice))
            ).scalar_one()
        (item,) = record.enrichment
        assert "ownership" not in item


class TestWorkMatchedByEitherId:
    async def test_google_books_only_entry_refuses_the_work_row(self, env):
        """The user has a Google-Books-only Book (no work ID, the same
        volume); another Book holds the work ID. The result names both IDs:
        the scan says owned, and the add (which resolves the work's Book)
        is refused the same way, never a second entry (review finding 4)."""
        factory, make_user, ids, _ = env
        alice = await make_user()
        await _own(factory, alice, ids, isbn_13=ids["other_13"], volume=ids["volume"])
        async with factory() as db:
            db.add(Book(open_library_work_id=ids["work"], title="Dune", author="F"))
            await db.commit()
        (found,) = await _search(factory, alice)
        assert (found.open_library_work_id, found.google_books_id) == (
            ids["work"],
            ids["volume"],
        )
        assert found.ownership == "owned_other_edition"
        assert await _refused(factory, alice, found) == "owned_other_edition"


class TestConcurrentAdds:
    async def test_the_add_that_loses_the_race_is_a_409(self, env):
        """Two adds of the same book at once both pass the library check;
        the loser's insert hits ``uq_user_books_user_book``. Reproduced by
        hiding the user's entry from the first library query, as if the
        other request committed just after it (review finding 3)."""
        factory, make_user, _ids, _ = env
        alice = await make_user()
        (found,) = await _search(factory, alice)
        await _wishlist(factory, alice, found)  # the request that won
        before = await _counts(factory)
        req = WishlistRequest.model_validate(found.model_dump(mode="json"))
        async with factory() as db:
            user = await db.get(User, alice)
            real, hidden = db.execute, {"left": 1}

            async def execute(stmt, *args, **kwargs):
                entity = stmt.column_descriptions[0].get("entity")
                if entity is UserBook and hidden["left"]:
                    hidden["left"] -= 1
                    result = MagicMock()
                    result.all.return_value = []
                    return result
                return await real(stmt, *args, **kwargs)

            db.execute = execute
            with pytest.raises(HTTPException) as exc:
                await user_books_api.add_to_wishlist.__wrapped__(
                    request=MagicMock(), req=req, current_user=user, db=db
                )
        assert hidden["left"] == 0
        assert (exc.value.status_code, exc.value.detail) == (409, "owned_same_edition")
        assert await _counts(factory) == before
