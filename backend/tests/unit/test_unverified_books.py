"""Unit tests for unverified entries (#616): ISBN normalisation, the
request schema and how a library row is serialised."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.api import user_books as user_books_api
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.schemas.user_book import (
    UnverifiedBookCreate,
    UserBookRead,
    normalise_user_isbn,
)
from app.services.enrichment import normalise_text

NOW = datetime(2026, 10, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("9780306406157", "9780306406157"),
        ("978-0-306-40615-7", "9780306406157"),
        ("978 0 306 40615 7", "9780306406157"),
        ("0306406152", "9780306406157"),
        ("0-306-40615-2", "9780306406157"),
        ("080442957X", "9780804429573"),
        ("080442957x", "9780804429573"),
    ],
)
def test_normalise_user_isbn(raw, expected):
    assert normalise_user_isbn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "٩٧٨٠٣٠٦٤٠٦١٥٧",  # Arabic-Indic digits
        "９７８０３０６４０６１５７",  # fullwidth digits
        "978-٠-306-40615-7",  # mixed scripts
    ],
)
def test_normalise_user_isbn_reads_other_scripts_as_ascii(raw):
    assert normalise_user_isbn(raw) == "9780306406157"


def test_normalise_user_isbn_rejects_digit_lookalikes():
    # Superscripts are not decimal digits.
    with pytest.raises(ValueError):
        normalise_user_isbn("978030640615⁷")


def test_schema_stores_the_ascii_isbn():
    req = UnverifiedBookCreate(title="T", author="A", isbn="٩٧٨٠٣٠٦٤٠٦١٥٧")
    assert req.isbn == "9780306406157" and req.isbn.isascii()


@pytest.mark.parametrize(
    "raw",
    ["9780306406158", "0306406153", "97803064061", "abcdefghij", "X306406152", ""],
)
def test_normalise_user_isbn_rejects(raw):
    with pytest.raises(ValueError):
        normalise_user_isbn(raw)


def test_schema_strips_and_drops_an_empty_isbn():
    req = UnverifiedBookCreate(title="  Pond  ", author=" A ", isbn="  ")
    assert (req.title, req.author, req.isbn) == ("Pond", "A", None)


def test_schema_refuses_unknown_fields():
    with pytest.raises(ValidationError):
        UnverifiedBookCreate(title="T", author="A", google_books_id="x")


def test_normalise_text():
    assert normalise_text("  Pond, NOTES!  ") == "pond notes"
    assert normalise_text(None) == ""


def _custom():
    return UserCustomBook(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="Pond Notes",
        author="A. Local",
        isbn_13="9780306406157",
        created_at=NOW,
        updated_at=NOW,
    )


def test_unverified_row_is_serialised_with_its_private_entry():
    custom = _custom()
    ub = UserBook(
        id=uuid.uuid4(),
        user_id=custom.user_id,
        custom_book_id=custom.id,
        status="reading",
        notes="n",
        rating=3,
        created_at=NOW,
        updated_at=NOW,
    )
    ub.custom_book = custom
    out = UserBookRead.model_validate(ub)
    assert out.unverified is True
    assert out.unverified_isbn_13 == "9780306406157"
    assert out.book.id == custom.id
    assert (out.book.title, out.book.author) == ("Pond Notes", "A. Local")
    assert out.book.open_library_work_id is None
    assert out.book.cover_url is None and out.book.editions == []
    assert (out.status, out.notes, out.rating) == ("reading", "n", 3)
    assert out.edition is None
    assert ub.unverified is True


def test_unverified_entry_has_no_language():
    custom = _custom()
    ub = UserBook(
        id=uuid.uuid4(),
        user_id=custom.user_id,
        custom_book_id=custom.id,
        status="wishlisted",
        created_at=NOW,
        updated_at=NOW,
    )
    ub.custom_book = custom
    out = UserBookRead.model_validate(ub)
    assert out.book.language is None
    assert out.model_dump(mode="json")["book"]["language"] is None


def test_any_custom_book_id_that_is_set_marks_an_unverified_entry():
    """The test is "is set", not "is a UUID": a row read through a driver or
    a fake that hands the id back as text is still the user's own entry."""
    custom = _custom()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        custom_book_id=str(custom.id),
        custom_book=custom,
        book=None,
        edition=None,
        status="wishlisted",
        wishlisted_at=None,
        purchased_at=None,
        started_at=None,
        finished_at=None,
        notes=None,
        rating=None,
        created_at=NOW,
        updated_at=NOW,
    )
    out = UserBookRead.model_validate(row)
    assert out.unverified is True and out.book.title == "Pond Notes"


def test_row_fields_follow_the_schema():
    """The fields read off a row come from the schema itself, so a field added
    to it is not silently dropped for want of an entry in a second list."""

    class WithExtra(UserBookRead):
        marker: str | None = None

    row = SimpleNamespace(
        id=uuid.uuid4(),
        custom_book_id=None,
        book=SimpleNamespace(
            id=uuid.uuid4(),
            title="Dune",
            author="F",
            description=None,
            cover_url=None,
            subjects=None,
            language="en",
            open_library_work_id=None,
            google_books_id=None,
            created_at=NOW,
            updated_at=NOW,
            editions=[],
            merged_into_id=None,  # not merged (#682)
        ),
        edition=None,
        status="read",
        wishlisted_at=None,
        purchased_at=None,
        started_at=None,
        finished_at=None,
        notes="n",
        rating=5,
        created_at=NOW,
        updated_at=NOW,
        marker="seen",
        # No user overrides (#682).
        title_override=None,
        author_override=None,
        cover_url_override=None,
    )
    assert WithExtra.model_validate(row).marker == "seen"


def test_catalog_row_is_not_unverified():
    book = SimpleNamespace(
        id=uuid.uuid4(),
        title="Dune",
        author="Frank Herbert",
        description=None,
        cover_url=None,
        subjects=None,
        language="en",
        open_library_work_id="OL1W",
        google_books_id=None,
        created_at=NOW,
        updated_at=NOW,
        editions=[],
        merged_into_id=None,  # not merged (#682)
    )
    ub = UserBook(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        book_id=book.id,
        status="wishlisted",
        created_at=NOW,
        updated_at=NOW,
    )
    ub.__dict__["book"] = book
    out = UserBookRead.model_validate(ub)
    assert out.unverified is False and out.unverified_isbn_13 is None
    assert out.book.title == "Dune"
    assert ub.unverified is False


async def test_delete_trashes_the_unverified_entry_too():
    """#683 (soft delete) replaces #616's hard delete: the library row and
    its unverified entry both go to the trash, nothing is deleted."""
    custom = _custom()
    ub = UserBook(id=uuid.uuid4(), user_id=custom.user_id, custom_book_id=custom.id)
    ub.custom_book = custom
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = ub
    db.execute = AsyncMock(return_value=result)
    db.delete = AsyncMock()
    db.commit = AsyncMock()

    await user_books_api.delete_user_book.__wrapped__(
        request=SimpleNamespace(),
        user_book_id=ub.id,
        current_user=SimpleNamespace(id=custom.user_id),
        db=db,
    )
    db.delete.assert_not_awaited()
    assert ub.deleted_at is not None
    assert custom.deleted_at == ub.deleted_at
    db.commit.assert_awaited_once()


# ─── The add service: lock mode and duplicate key ───────────────────────────


def test_duplicate_key_of_punctuation_only_text_is_not_empty():
    from app.services.unverified_books import _key

    assert _key("!!!", "A") != _key("???", "A")
    assert _key("!!!", "A") == _key(" !!! ", "a")
    assert _key("...", "—") != _key("...", "…")
    # Text that normalises to something still compares by that.
    assert _key("Pond, Notes!", "A") == _key("pond notes", "a")


async def test_add_locks_the_user_row_for_no_key_update_only():
    """FOR UPDATE would also block the FOR KEY SHARE lock that an insert into
    a table referencing users takes; FOR NO KEY UPDATE serialises the adds
    without that."""
    from sqlalchemy.dialects import postgresql

    from app.services.unverified_books import UnverifiedBookService

    first = MagicMock()
    rows = MagicMock()
    rows.all.return_value = []
    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[first, rows])
    await UnverifiedBookService().add(
        db, uuid.uuid4(), UnverifiedBookCreate(title="T", author="A")
    )
    lock = db.execute.await_args_list[0].args[0]
    sql = str(lock.compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR NO KEY UPDATE")
