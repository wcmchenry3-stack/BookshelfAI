"""resolve_google_user against real Postgres (#688).

Needs DATABASE_URL pointing at a Postgres database.
"""

import uuid

import pytest
from sqlalchemy import select

from app.auth.identity import AccountConflictError, resolve_google_user
from app.models.user import User

pytestmark = pytest.mark.integration


def _tag() -> str:
    return uuid.uuid4().hex[:10]


async def _add(db, email, google_id):
    user = User(email=email, google_id=google_id, display_name="Before")
    db.add(user)
    await db.flush()
    return user


async def _resolve(db, google_id, email):
    return await resolve_google_user(
        db,
        google_id=google_id,
        email=email,
        display_name="After",
        avatar_url=None,
    )


async def test_email_change_at_google_keeps_user_and_updates_email(db_session):
    t = _tag()
    user = await _add(db_session, f"old-{t}@example.com", f"sub-{t}")

    got = await _resolve(db_session, f"sub-{t}", f"new-{t}@example.com")
    await db_session.flush()

    assert got.id == user.id
    rows = (
        (await db_session.execute(select(User).where(User.google_id == f"sub-{t}")))
        .scalars()
        .all()
    )
    assert [(r.id, r.email, r.display_name) for r in rows] == [
        (user.id, f"new-{t}@example.com", "After")
    ]


async def test_email_held_by_other_subject_is_conflict_and_victim_unchanged(
    db_session,
):
    t = _tag()
    victim = await _add(db_session, f"victim-{t}@example.com", f"victim-{t}")

    with pytest.raises(AccountConflictError):
        await _resolve(db_session, f"attacker-{t}", f"victim-{t}@example.com")
    await db_session.flush()

    await db_session.refresh(victim)
    assert (victim.email, victim.google_id, victim.display_name) == (
        f"victim-{t}@example.com",
        f"victim-{t}",
        "Before",
    )
    attacker = (
        await db_session.execute(select(User).where(User.google_id == f"attacker-{t}"))
    ).scalar_one_or_none()
    assert attacker is None


async def test_new_email_already_taken_keeps_stored_email(db_session):
    t = _tag()
    user = await _add(db_session, f"a-{t}@example.com", f"a-{t}")
    await _add(db_session, f"b-{t}@example.com", f"b-{t}")

    got = await _resolve(db_session, f"a-{t}", f"b-{t}@example.com")
    await db_session.flush()  # no unique violation

    assert got.id == user.id
    assert got.email == f"a-{t}@example.com"


async def test_new_subject_and_email_creates_user(db_session):
    t = _tag()
    got = await _resolve(db_session, f"fresh-{t}", f"fresh-{t}@example.com")
    await db_session.flush()
    assert got.id is not None
    assert got.google_id == f"fresh-{t}"
