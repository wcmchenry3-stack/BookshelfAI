"""resolve_google_user against real Postgres (#688).

Needs DATABASE_URL pointing at a Postgres database.
"""

import uuid

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import identity
from app.auth.identity import (
    AccountConflictError,
    is_unique_violation,
    resolve_and_commit_google_user,
    resolve_google_user,
)
from app.core.config import settings
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


async def test_case_variant_email_with_other_subject_is_conflict(db_session):
    t = _tag()
    victim = await _add(db_session, f"Victim-{t}@Example.com", f"victim-{t}")

    with pytest.raises(AccountConflictError):
        await _resolve(db_session, f"attacker-{t}", f"victim-{t}@example.COM")
    await db_session.flush()

    await db_session.refresh(victim)
    assert victim.email == f"Victim-{t}@Example.com"
    assert (
        await db_session.execute(select(User).where(User.google_id == f"attacker-{t}"))
    ).scalar_one_or_none() is None


async def test_case_only_change_for_same_subject_keeps_user(db_session):
    t = _tag()
    user = await _add(db_session, f"alice-{t}@example.com", f"alice-{t}")

    got = await _resolve(db_session, f"alice-{t}", f"Alice-{t}@Example.com")
    await db_session.flush()

    assert got.id == user.id
    assert got.email == f"Alice-{t}@Example.com"


@pytest.fixture
async def sessions():
    """Two independent committing sessions; every row they made is removed."""
    engine = create_async_engine(settings.async_database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    tag = _tag()
    yield maker, tag
    async with maker() as s:
        await s.execute(delete(User).where(User.google_id.like(f"%{tag}%")))
        await s.commit()
    await engine.dispose()


def _race(monkeypatch, maker, competitor: User):
    """After the first lookup, a concurrent sign-in commits ``competitor``,
    so this session's commit hits a real unique violation."""
    real = identity.resolve_google_user
    calls = {"n": 0}

    async def racing(db, **kwargs):
        user = await real(db, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            async with maker() as other:
                other.add(competitor)
                await other.commit()
        return user

    monkeypatch.setattr(identity, "resolve_google_user", racing)
    return calls


async def test_double_tap_same_new_subject_signs_in_once(sessions, monkeypatch):
    maker, t = sessions
    winner = User(email=f"tap-{t}@example.com", google_id=f"tap-{t}")
    calls = _race(monkeypatch, maker, winner)

    async with maker() as db:
        got = await resolve_and_commit_google_user(
            db,
            google_id=f"tap-{t}",
            email=f"tap-{t}@example.com",
            display_name="After",
            avatar_url=None,
        )
    assert calls["n"] == 2
    assert got.id == winner.id
    async with maker() as db:
        rows = (
            (await db.execute(select(User).where(User.google_id == f"tap-{t}")))
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_email_taken_concurrently_by_other_subject_is_conflict(
    sessions, monkeypatch
):
    maker, t = sessions
    other = User(email=f"race-{t}@example.com", google_id=f"other-{t}")
    _race(monkeypatch, maker, other)

    async with maker() as db:
        with pytest.raises(AccountConflictError):
            await resolve_and_commit_google_user(
                db,
                google_id=f"mine-{t}",
                # Same case; the case-variant race is
                # test_case_variant_email_taken_concurrently_is_conflict.
                email=f"race-{t}@example.com",
                display_name=None,
                avatar_url=None,
            )
    async with maker() as db:
        mine = (
            await db.execute(select(User).where(User.google_id == f"mine-{t}"))
        ).scalar_one_or_none()
    assert mine is None


async def test_real_postgres_errors_are_classified(sessions):
    """The asyncpg-wrapped unique violation is retried; NOT NULL is not."""
    maker, t = sessions
    async with maker() as db:
        db.add(User(email=f"cls-{t}@example.com", google_id=f"cls-{t}"))
        await db.commit()
        db.add(User(email=f"cls-{t}@example.com", google_id=f"cls2-{t}"))
        with pytest.raises(IntegrityError) as dup:
            await db.commit()
        await db.rollback()
        db.add(User(email=None, google_id=f"cls3-{t}"))
        with pytest.raises(IntegrityError) as not_null:
            await db.commit()
        await db.rollback()
    assert is_unique_violation(dup.value)
    assert not is_unique_violation(not_null.value)


async def test_case_only_change_to_another_rows_spelling_keeps_stored_email(db_session):
    # Codex on #702 covered a known subject reporting another row's exact
    # spelling when case-duplicate rows existed. Since #705 the schema
    # (uq_users_email_lower) refuses the second, case-variant row, so that
    # state can no longer arise.
    t = _tag()
    await _add(db_session, f"alice-{t}@example.com", f"a-{t}")

    savepoint = await db_session.begin_nested()
    with pytest.raises(IntegrityError, match="uq_users_email_lower") as dup:
        await _add(db_session, f"Alice-{t}@example.com", f"b-{t}")
    await savepoint.rollback()
    assert is_unique_violation(dup.value)


async def test_case_variant_email_taken_concurrently_is_conflict(sessions, monkeypatch):
    """#705: after this sign-in's lookup found no holder, another subject
    commits a case variant of the same address. The commit hits
    uq_users_email_lower; the retry finds the winner and reports a conflict.
    """
    maker, t = sessions
    other = User(email=f"Race-{t}@Example.com", google_id=f"other-{t}")
    _race(monkeypatch, maker, other)

    async with maker() as db:
        with pytest.raises(AccountConflictError):
            await resolve_and_commit_google_user(
                db,
                google_id=f"mine-{t}",
                email=f"race-{t}@example.com",
                display_name=None,
                avatar_url=None,
            )
    async with maker() as db:
        rows = (
            (
                await db.execute(
                    select(User.google_id).where(
                        func.lower(User.email) == f"race-{t}@example.com"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [f"other-{t}"]
