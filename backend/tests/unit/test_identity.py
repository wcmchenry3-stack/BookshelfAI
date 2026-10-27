"""resolve_google_user matches by google_id, never by email (#688)."""

import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth.identity import (
    AccountConflictError,
    email_allowed,
    resolve_and_commit_google_user,
    resolve_google_user,
)
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


async def test_case_only_email_change_is_stored_when_no_other_holder():
    user = _user("alice@example.com", "111")
    db = _db(user, None)
    got = await _resolve(db, google_id="111", email="Alice@Example.com")
    assert got is user
    assert user.email == "Alice@Example.com"  # stored as Google reports it
    where = str(db.execute.await_args_list[1].args[0].whereclause)
    assert "users.id != :id_1" in where  # the user itself is not a holder


async def test_case_only_change_keeps_stored_email_when_another_row_holds_it():
    # Codex on #702: rows differing only by case can exist; storing the other
    # row's exact spelling would fail the unique constraint and 409 a known user.
    user = _user("alice@example.com", "111")
    other = _user("Alice@example.com", "222")
    db = _db(user, other)
    got = await _resolve(db, google_id="111", email="Alice@example.com")
    assert got is user
    assert user.email == "alice@example.com"


async def test_case_variant_of_existing_email_is_a_conflict():
    victim = _user("victim@example.com", "999")
    db = _db(None, victim)
    with pytest.raises(AccountConflictError):
        await _resolve(db, google_id="attacker", email="Victim@EXAMPLE.com")
    where = str(db.execute.await_args_list[1].args[0].whereclause)
    assert where == "lower(users.email) = :lower_1"
    assert victim.email == "victim@example.com"


async def test_changed_email_holder_lookup_ignores_case():
    user = _user("old@example.com", "111")
    other = _user("new@example.com", "222")
    db = _db(user, other)
    await _resolve(db, google_id="111", email="NEW@example.com")
    assert user.email == "old@example.com"


@pytest.mark.parametrize(
    ("email", "allowlist", "expected"),
    [
        ("a@x.com", [], True),
        ("A@X.com", ["a@x.com"], True),
        ("a@x.com", ["A@X.COM"], True),
        ("b@x.com", ["a@x.com"], False),
    ],
)
def test_email_allowed_ignores_case(email, allowlist, expected):
    assert email_allowed(email, allowlist) is expected


def _committing_db(*lookups, commits):
    db = _db(*lookups)
    db.commit = AsyncMock(side_effect=commits)
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


class _PgError(Exception):
    """Shaped like SQLAlchemy's asyncpg adapter error (``exc.orig``)."""

    def __init__(self, sqlstate):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _dup():
    return IntegrityError("INSERT", {}, _PgError("23505"))


async def test_resolve_and_commit_retries_once_and_finds_the_winner():
    winner = _user("new@example.com", "111")
    db = _committing_db(None, None, winner, commits=[_dup(), None])
    got = await resolve_and_commit_google_user(
        db,
        google_id="111",
        email="new@example.com",
        display_name=None,
        avatar_url=None,
    )
    assert got is winner
    db.rollback.assert_awaited_once()
    db.refresh.assert_awaited_once_with(winner)


async def test_resolve_and_commit_conflicts_when_email_taken_meanwhile():
    other = _user("new@example.com", "222")
    db = _committing_db(None, None, None, other, commits=[_dup()])
    with pytest.raises(AccountConflictError):
        await resolve_and_commit_google_user(
            db,
            google_id="111",
            email="new@example.com",
            display_name=None,
            avatar_url=None,
        )
    db.rollback.assert_awaited_once()


async def test_resolve_and_commit_gives_up_after_two_violations():
    db = _committing_db(None, None, None, None, commits=[_dup(), _dup()])
    with pytest.raises(AccountConflictError):
        await resolve_and_commit_google_user(
            db,
            google_id="111",
            email="new@example.com",
            display_name=None,
            avatar_url=None,
        )
    assert db.rollback.await_count == 2


@pytest.mark.parametrize("sqlstate", ["23502", "23503"])  # NOT NULL, FK
async def test_resolve_and_commit_reraises_non_unique_integrity_errors(sqlstate):
    db = _committing_db(
        None, None, commits=[IntegrityError("I", {}, _PgError(sqlstate))]
    )
    with pytest.raises(IntegrityError):
        await resolve_and_commit_google_user(
            db,
            google_id="111",
            email="new@example.com",
            display_name=None,
            avatar_url=None,
        )
    db.rollback.assert_awaited_once()
    assert db.execute.await_count == 2  # no retry


def test_is_unique_violation_reads_sqlstate_pgcode_and_cause():
    from app.auth.identity import is_unique_violation

    class _PgCode(Exception):
        pgcode = "23505"

    cause_only = Exception("wrapped")
    cause_only.__cause__ = _PgError("23505")

    assert is_unique_violation(IntegrityError("I", {}, _PgError("23505")))
    assert is_unique_violation(IntegrityError("I", {}, _PgCode()))
    assert is_unique_violation(IntegrityError("I", {}, cause_only))
    assert not is_unique_violation(IntegrityError("I", {}, _PgError("23502")))
    assert not is_unique_violation(IntegrityError("I", {}, Exception("x")))


async def test_email_lookup_takes_the_oldest_holder():
    db = _db(None, _user("victim@example.com", "999"))
    with pytest.raises(AccountConflictError):
        await _resolve(db, google_id="attacker", email="victim@example.com")
    sql = str(db.execute.await_args_list[1].args[0])
    assert "ORDER BY users.created_at, users.id" in sql
    assert "LIMIT" in sql
