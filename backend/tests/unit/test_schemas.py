"""Unit tests for Pydantic schemas."""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.book import EditionRead, EnrichedBook
from app.schemas.user_book import UserBookUpdate


def _now() -> datetime:
    return datetime.now(tz=UTC)


class TestEditionRead:
    def test_minimal(self):
        e = EditionRead(id=uuid.uuid4(), book_id=uuid.uuid4(), created_at=_now())
        assert e.isbn_13 is None
        assert e.format is None

    def test_invalid_format(self):
        with pytest.raises(ValidationError):
            EditionRead(
                id=uuid.uuid4(),
                book_id=uuid.uuid4(),
                created_at=_now(),
                format="magazine",  # not in allowed set
            )


class TestUserBookUpdate:
    def test_rating_out_of_range(self):
        with pytest.raises(ValidationError):
            UserBookUpdate(rating=6)

    def test_rating_zero(self):
        with pytest.raises(ValidationError):
            UserBookUpdate(rating=0)

    def test_rating_valid(self):
        u = UserBookUpdate(rating=3)
        assert u.rating == 3

    def test_all_none_is_valid(self):
        u = UserBookUpdate()
        assert u.status is None
        assert u.notes is None
        assert u.rating is None


class TestEnrichedBook:
    def test_requires_title_author_confidence(self):
        with pytest.raises(ValidationError):
            EnrichedBook(title="Dune")  # missing author and confidence

    def test_valid(self):
        e = EnrichedBook(title="Dune", author="Frank Herbert", confidence=0.95)
        assert e.already_in_library is False
        assert e.editions == []
        assert e.subjects == []
