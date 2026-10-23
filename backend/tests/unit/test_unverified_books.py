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


async def test_delete_removes_the_unverified_entry_too():
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
    assert [c.args[0] for c in db.delete.await_args_list] == [ub, custom]
    db.commit.assert_awaited_once()
