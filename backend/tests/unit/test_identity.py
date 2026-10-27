"""resolve_google_user matches by google_id, never by email (#688)."""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.identity import AccountConflictError, resolve_google_user
from app.models.user import User


def _user(email: str, google_id: str) -> User:
    return User(id=uuid.uuid4(), email=email, google_id=google_id, display_name="Old")


def _db(*lookups):
    """A fake session whose successive ``execute`` calls return ``lookups``."""
    results = []
    for value in lookups:
        r = MagicMock()
        r.scalar_one_or_none.return_value = value
        results.append(r)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=results)
    return db


async def _resolve(db, *, google_id="111", email="new@example.com"):
    return await resolve_google_user(
        db,
        google_id=google_id,
        email=email,
        display_name="New Name",
        avatar_url="https://a/new.png",
    )


async def test_known_google_id_with_same_email_refreshes_profile():
    user = _user("same@example.com", "111")
    db = _db(user)
    got = await _resolve(db, email="same@example.com")
    assert got is user
    assert (user.display_name, user.avatar_url) == ("New Name", "https://a/new.png")
    assert db.execute.await_count == 1  # no email lookup at all
    db.add.assert_not_called()


async def test_known_google_id_with_changed_email_updates_email():
    user = _user("old@example.com", "111")
    db = _db(user, None)
    got = await _resolve(db, email="new@example.com")
    assert got is user
    assert user.email == "new@example.com"
    assert user.google_id == "111"
    db.add.assert_not_called()


async def test_changed_email_held_by_another_user_keeps_stored_email(caplog):
    user = _user("old@example.com", "111")
    other = _user("new@example.com", "222")
    db = _db(user, other)
    with caplog.at_level(logging.INFO):
        got = await _resolve(db, email="new@example.com")
    assert got is user
    assert user.email == "old@example.com"
    assert other.email == "new@example.com"
    assert str(user.id) in caplog.text and str(other.id) in caplog.text
    assert "@example.com" not in caplog.text


async def test_unknown_google_id_and_unknown_email_creates_user():
    db = _db(None, None)
    got = await _resolve(db, google_id="333", email="fresh@example.com")
    db.add.assert_called_once_with(got)
    assert (got.google_id, got.email) == ("333", "fresh@example.com")
    assert got.display_name == "New Name"


async def test_unknown_google_id_with_taken_email_is_a_conflict(caplog):
    victim = _user("victim@example.com", "999")
    db = _db(None, victim)
    with caplog.at_level(logging.INFO), pytest.raises(AccountConflictError):
        await _resolve(db, google_id="attacker", email="victim@example.com")
    db.add.assert_not_called()
    assert (victim.email, victim.google_id, victim.display_name) == (
        "victim@example.com",
        "999",
        "Old",
    )
    assert str(victim.id) in caplog.text
    assert "victim@example.com" not in caplog.text


async def test_empty_google_id_is_rejected():
    db = _db()
    with pytest.raises(ValueError):
        await _resolve(db, google_id="")
    db.execute.assert_not_called()
