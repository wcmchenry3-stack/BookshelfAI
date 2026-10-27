"""uq_users_email_lower against real PostgreSQL (#705).

- Migration 0022 refuses to run while case-duplicate emails exist (the
  message has a count, never an email, and nothing is created); once they are
  gone it adds the unique index on ``lower(email)``, which then refuses a
  case variant with a violation ``is_unique_violation`` recognises; the
  downgrade drops it. Runs in a rolled-back transaction.
- Two concurrent first sign-ins through POST /auth/google from different
  Google subjects, with emails differing only in case: one 200, one 409
  ``account_conflict``, and exactly one row for the mailbox.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import identity
from app.auth.identity import is_unique_violation
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.user import User

pytestmark = pytest.mark.integration

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0022_users_email_lower_unique.py"
)
INDEX = "uq_users_email_lower"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index(connection) -> dict | None:
    for ix in inspect(connection).get_indexes("users"):
        if ix["name"] == INDEX:
            return ix
    return None


def _user(connection, email) -> uuid.UUID:
    uid = uuid.uuid4()
    connection.execute(
        sa.insert(User).values(id=uid, email=email, google_id=f"g-{uid}")
    )
    return uid


def _lock_timeout_is_default(connection) -> bool:
    """lock_timeout is the session's default again (not 0022's 5 s)."""
    return connection.execute(
        sa.text(
            "SELECT setting = reset_val FROM pg_settings WHERE name = 'lock_timeout'"
        )
    ).scalar_one()


def _holds_share_lock_on_users(connection) -> bool:
    return connection.execute(
        sa.text(
            "SELECT bool_or(l.mode = 'ShareLock' AND l.granted) FROM pg_locks l "
            "JOIN pg_class c ON c.oid = l.relation "
            "WHERE c.relname = 'users' AND l.pid = pg_backend_pid()"
        )
    ).scalar_one()


def test_model_declares_the_index_the_migration_creates():
    migration = _load_migration()
    ix = next(i for i in User.__table__.indexes if i.name == INDEX)
    assert migration.INDEX == INDEX
    assert ix.unique is True
    assert [str(e) for e in ix.expressions] == ["lower(email)"]
    # The case-sensitive unique constraint is kept (see the migration).
    assert User.__table__.c.email.unique is True


def _cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0022", "0021")
    context = MigrationContext.configure(connection)
    tag = uuid.uuid4().hex[:10]

    with Operations.context(context):
        if _index(connection) is not None:
            migration.downgrade()
        assert _index(connection) is None
        assert _lock_timeout_is_default(connection)

        # Two addresses each held twice, differing only in case; one unique.
        dup_a = _user(connection, f"Dup-a-{tag}@Example.com")
        _user(connection, f"dup-a-{tag}@example.com")
        dup_b = _user(connection, f"DUP-B-{tag}@example.com")
        _user(connection, f"dup-b-{tag}@example.com")
        _user(connection, f"single-{tag}@example.com")

        assert migration.count_case_duplicates(connection) == 2
        with pytest.raises(RuntimeError) as refused:
            migration.upgrade()
        message = str(refused.value)
        assert "2 email address(es)" in message
        assert "@" not in message and tag not in message
        assert _index(connection) is None  # nothing was created

        # The operator resolves the duplicates; the upgrade then succeeds.
        connection.execute(sa.delete(User).where(User.id.in_([dup_a, dup_b])))
        assert migration.count_case_duplicates(connection) == 0
        migration.upgrade()
        ix = _index(connection)
        assert ix is not None and ix["unique"]
        assert ix["expressions"] == ["lower((email)::text)"]
        # Writes to users wait until the run commits, so no duplicate could
        # slip in between the pre-check and the build.
        assert _holds_share_lock_on_users(connection)
        assert _lock_timeout_is_default(connection)
        # The case-sensitive constraint is still there.
        assert any(
            uc["column_names"] == ["email"]
            for uc in inspect(connection).get_unique_constraints("users")
        )

        # A case variant is now a unique violation the sign-in retry treats
        # as a race.
        savepoint = connection.begin_nested()
        with pytest.raises(IntegrityError, match=INDEX) as dup:
            _user(connection, f"SINGLE-{tag}@example.com")
        savepoint.rollback()
        assert is_unique_violation(dup.value)

        # Downgrade drops the index only: case variants are accepted again.
        migration.downgrade()
        assert _index(connection) is None
        assert _lock_timeout_is_default(connection)
        _user(connection, f"SINGLE-{tag}@example.com")


async def test_migration_0022_precheck_upgrade_downgrade_cycle():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


def test_migration_0022_refuses_offline_sql():
    migration = _load_migration()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True}
    )
    with Operations.context(context), pytest.raises(RuntimeError, match="offline"):
        migration.upgrade()


async def test_concurrent_case_variant_first_sign_ins_one_row_and_409(monkeypatch):
    """Both requests look up the address (no holder yet) before either
    commits, so the race is real: the loser's INSERT waits on the winner's
    index entry, then fails with 23505 on uq_users_email_lower, is retried,
    finds the winner's row and answers 409."""
    tag = uuid.uuid4().hex[:10]
    claims = {
        "A": {
            "sub": f"sub-a-{tag}",
            "email": f"Me-{tag}@X.com",
            "email_verified": True,
        },
        "B": {
            "sub": f"sub-b-{tag}",
            "email": f"me-{tag}@x.com",
            "email_verified": True,
        },
    }
    engine = create_async_engine(settings.async_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _db():
        async with maker() as session:
            yield session

    barrier = asyncio.Barrier(2)
    real = identity.resolve_google_user
    calls: dict[str, int] = {}

    async def lookup_then_wait(db, **kwargs):
        user = await real(db, **kwargs)
        sub = kwargs["google_id"]
        calls[sub] = calls.get(sub, 0) + 1
        if calls[sub] == 1:  # only the first lookup of each request
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return user

    async def verify(token):
        return claims[token]

    monkeypatch.setattr(identity, "resolve_google_user", lookup_then_wait)
    monkeypatch.setattr(settings, "allowed_emails", "")
    app.dependency_overrides[get_db] = _db
    try:
        with patch("app.api.auth.verify_google_id_token", side_effect=verify):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp_a, resp_b = await asyncio.gather(
                    client.post("/auth/google", json={"id_token": "A"}),
                    client.post("/auth/google", json={"id_token": "B"}),
                )
        statuses = sorted([resp_a.status_code, resp_b.status_code])
        assert statuses == [200, 409], (resp_a.text, resp_b.text)
        loser = resp_a if resp_a.status_code == 409 else resp_b
        assert loser.json() == {"detail": "account_conflict"}
        # The loser's commit failed once and its lookup ran again.
        assert sorted(calls.values()) == [1, 2]

        async with maker() as db:
            rows = (
                (
                    await db.execute(
                        select(User.google_id).where(
                            func.lower(User.email) == f"me-{tag}@x.com"
                        )
                    )
                )
                .scalars()
                .all()
            )
        winner = "A" if resp_a.status_code == 200 else "B"
        assert rows == [claims[winner]["sub"]]
    finally:
        app.dependency_overrides.pop(get_db, None)
        async with maker() as db:
            await db.execute(delete(User).where(User.google_id.like(f"%{tag}%")))
            await db.commit()
        await engine.dispose()
