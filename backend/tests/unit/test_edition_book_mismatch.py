"""#686: an edition_id must belong to the book_id it is sent with."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.edition_check import (
    EDITION_BOOK_MISMATCH,
    EditionBookMismatchError,
    ensure_edition_belongs_to_book,
)
from tests._edition_fake import (
    BOOK_A,
    BOOK_B,
    ED_OF_A,
    ED_OF_B,
    UNKNOWN_ED,
    EditionFakeSession,
    client_for,
    result,
)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


class TestHelper:
    async def test_none_edition_is_accepted(self):
        await ensure_edition_belongs_to_book(EditionFakeSession(), BOOK_A, None)

    async def test_matching_pair_is_accepted(self):
        await ensure_edition_belongs_to_book(EditionFakeSession(), BOOK_A, ED_OF_A)

    async def test_other_books_edition_is_refused(self):
        with pytest.raises(EditionBookMismatchError):
            await ensure_edition_belongs_to_book(EditionFakeSession(), BOOK_A, ED_OF_B)

    async def test_unknown_edition_is_refused(self):
        with pytest.raises(EditionBookMismatchError):
            await ensure_edition_belongs_to_book(
                EditionFakeSession(), BOOK_A, UNKNOWN_ED
            )


class TestPurchased:
    def test_mismatch_is_422_and_writes_nothing(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(ED_OF_B)}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == EDITION_BOOK_MISMATCH == "edition_book_mismatch"
        assert db.added == [] and not db.committed

    def test_unknown_edition_is_422(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(UNKNOWN_ED)}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "edition_book_mismatch"

    def test_mismatch_with_isbn_is_still_422(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased",
            json={
                "book_id": str(BOOK_A),
                "edition_id": str(ED_OF_B),
                "isbn_13": "9780441013593",
            },
        )
        assert resp.status_code == 422
        assert db.added == []

    def test_matching_pair_is_created(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(ED_OF_A)}
        )
        assert resp.status_code == 201
        assert db.added[0].edition_id == ED_OF_A
        assert db.committed

    def test_null_edition_still_works(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": None}
        )
        assert resp.status_code == 201
        assert db.added[0].edition_id is None

    def test_other_book_with_its_own_edition_is_accepted(self):
        db = EditionFakeSession()
        resp = client_for(db).post(
            "/purchased", json={"book_id": str(BOOK_B), "edition_id": str(ED_OF_B)}
        )
        assert resp.status_code == 201

    def test_requires_auth(self):
        resp = TestClient(app).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(ED_OF_A)}
        )
        assert resp.status_code == 401

    def test_malformed_edition_id_is_a_validation_error(self):
        resp = client_for(EditionFakeSession()).post(
            "/purchased", json={"book_id": str(BOOK_A), "edition_id": "not-a-uuid"}
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] != "edition_book_mismatch"


class TestOtherWritePathsTakeNoEditionId:
    """PATCH has no edition_id field, so a client cannot set one there."""

    def test_patch_ignores_edition_id(self):
        db = EditionFakeSession()
        ub = db.row
        ub.edition_id = None

        async def _execute(stmt):
            return result(ub)

        async def _noop(*a, **k):
            return None

        db.execute = _execute
        db.commit = _noop
        db.refresh = _noop
        resp = client_for(db).patch(
            f"/user-books/{ub.id}", json={"notes": "x", "edition_id": str(ED_OF_B)}
        )
        assert resp.status_code == 200
        assert ub.edition_id is None
