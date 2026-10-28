"""#686: the edition/book check runs on the server whatever the client
sends: a forged pair, only an edition_id, an edition of another book, or an
unknown edition never reaches the database.

Run on its own with:  pytest -m security tests/security/test_edition_book_authority.py
"""

import pytest

from app.main import app
from tests._edition_fake import (
    BOOK_A,
    BOOK_B,
    ED_OF_A,
    ED_OF_B,
    UNKNOWN_ED,
    EditionFakeSession,
    client_for,
)

pytestmark = pytest.mark.security


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "body",
    [
        {"book_id": str(BOOK_A), "edition_id": str(ED_OF_B)},  # forged pair
        {"book_id": str(BOOK_B), "edition_id": str(ED_OF_A)},  # forged pair
        {"book_id": str(BOOK_A), "edition_id": str(UNKNOWN_ED)},
        {
            "book_id": str(BOOK_A),
            "edition_id": str(ED_OF_B),
            "isbn_13": "9780441013593",
        },
        {"book_id": str(BOOK_A), "edition_id": str(ED_OF_B), "status": "read"},
    ],
)
def test_forged_pair_is_refused_and_nothing_is_written(body):
    db = EditionFakeSession()
    resp = client_for(db).post("/purchased", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "edition_book_mismatch"
    assert db.added == [] and not db.committed


def test_only_edition_id_cannot_pick_a_book():
    """book_id is required: an edition_id alone is not enough to link."""
    db = EditionFakeSession()
    resp = client_for(db).post("/purchased", json={"edition_id": str(ED_OF_B)})
    assert resp.status_code == 422
    assert db.added == [] and not db.committed


def test_valid_pair_is_accepted():
    db = EditionFakeSession()
    resp = client_for(db).post(
        "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(ED_OF_A)}
    )
    assert resp.status_code == 201


def test_refusal_does_not_reveal_whether_the_edition_exists():
    c = client_for(EditionFakeSession())
    other = c.post(
        "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(ED_OF_B)}
    )
    unknown = c.post(
        "/purchased", json={"book_id": str(BOOK_A), "edition_id": str(UNKNOWN_ED)}
    )
    assert other.status_code == unknown.status_code == 422
    assert other.json() == unknown.json()
