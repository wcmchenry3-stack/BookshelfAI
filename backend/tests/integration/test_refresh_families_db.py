"""Refresh-token families and reuse detection against real Postgres (#690).

- Migration 0022 adds family_id / used_at / replaced_by_id / displaced_at,
  backfills every existing row as its own family (one UPDATE) and drops them
  again, leaving no difference to the models. Runs in a rolled-back
  transaction.
- Two concurrent refreshes with one token (row lock): the second is a race,
  not reuse (no revocation, no alert); it gets a fresh pair and displaces the
  first one's, so a thief who wins the race is caught at the next refresh.
- A retry within the grace window gets a pair that displaces the successor;
  a displaced token, or a second retry, is reuse.
- Reuse after the window revokes every refresh row, bumps token_version and
  sends one Sentry warning.
- The cleanup purge deletes expired and revoked-unrotated rows in batches and
  keeps rotated ones; deleting a successor nulls the pointer to it. It skips,
  rather than waits for, a row whose predecessor a replay has locked.

Needs DATABASE_URL pointing at a Postgres database.
"""

import asyncio
import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jwt
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import refresh_family
from app.core.config import settings
from app.core.database import Base, get_db
from app.main import app
from app.models.refresh_token import RefreshToken
from app.models.user import User
from tests.integration import test_token_version_db as _tv
from tests.unit._sentry_capture import captured_app_events

pytestmark = pytest.mark.integration

# Fixtures and helpers shared with the #689 tests (same engine, users and
# _PausingSession hook for interleaving).
_jwt_keys = _tv._jwt_keys
env = _tv.env
_auth = _tv._auth
_PausingSession = _tv._PausingSession

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0022_refresh_token_families.py"
)
COLUMNS = ("family_id", "used_at", "replaced_by_id", "displaced_at")


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0022", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_cycle(connection) -> None:
    migration = _load_migration()
    assert (migration.revision, migration.down_revision) == ("0022", "0021")
    context = MigrationContext.configure(connection)

    def columns() -> dict:
        return {c["name"]: c for c in inspect(connection).get_columns("refresh_tokens")}

    def diffs() -> list:
        return [
            d
            for d in compare_metadata(context, Base.metadata)
            if any(c in repr(d) for c in COLUMNS)
        ]

    with Operations.context(context):
        assert set(COLUMNS) <= set(columns())
        assert diffs() == []

        migration.downgrade()
        assert not set(COLUMNS) & set(columns())

        # Rows from before the migration: each becomes its own family.
        uid = uuid.uuid4()
        connection.execute(
            sa.text("INSERT INTO users (id, email, google_id) VALUES (:id, :e, :g)"),
            {"id": uid, "e": f"{uid}@example.com", "g": f"g-{uid}"},
        )
        for n in range(5):
            connection.execute(
                sa.text(
                    "INSERT INTO refresh_tokens (id, jti, user_id, expires_at) "
                    "VALUES (:id, :jti, :uid, now() + interval '1 day')"
                ),
                {"id": uuid.uuid4(), "jti": f"{uid}-{n}", "uid": uid},
            )

        # One UPDATE backfills every row.
        updates: list[str] = []

        def _count(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith("UPDATE REFRESH_TOKENS"):
                updates.append(statement)

        sa.event.listen(connection, "before_cursor_execute", _count)
        try:
            migration.upgrade()
        finally:
            sa.event.remove(connection, "before_cursor_execute", _count)
        assert len(updates) == 1

        cols = columns()
        for name in COLUMNS:
            assert cols[name]["nullable"] is True
        assert diffs() == []
        rows = connection.execute(
            sa.text("SELECT id, family_id FROM refresh_tokens WHERE user_id = :u"),
            {"u": uid},
        ).all()
        assert len(rows) == 5
        assert all(family == row_id for row_id, family in rows)
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM refresh_tokens WHERE family_id IS NULL")
            ).scalar_one()
            == 0
        )
        (fk,) = [
            fk
            for fk in inspect(connection).get_foreign_keys("refresh_tokens")
            if fk["constrained_columns"] == ["replaced_by_id"]
        ]
        assert fk["referred_table"] == "refresh_tokens"
        assert fk["options"].get("ondelete") == "SET NULL"
        assert "ix_refresh_tokens_replaced_by_id" in {
            i["name"] for i in inspect(connection).get_indexes("refresh_tokens")
        }

        # And down again cleanly.
        migration.downgrade()
        assert not set(COLUMNS) & set(columns())
        migration.upgrade()
        assert diffs() == []


async def test_migration_0022_upgrade_downgrade_cycle_with_backfill():
    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.run_sync(_migration_cycle)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


def _jti(token: str) -> str:
    return jwt.decode(token, settings.jwt_public_key, algorithms=["RS256"])["jti"]


async def _rows(factory, user_id) -> dict[str, RefreshToken]:
    async with factory() as db:
        rows = (
            (
                await db.execute(
                    select(RefreshToken).where(RefreshToken.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    return {r.jti: r for r in rows}


async def _backdate(factory, jti: str, seconds: int) -> None:
    async with factory() as db:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti)
            .values(used_at=RefreshToken.used_at - timedelta(seconds=seconds))
        )
        await db.commit()


def _warnings(events) -> list:
    return [e for e in events if e.get("level") == "warning"]


async def test_rotation_inherits_family_from_a_row_without_one(env):
    """make_user writes a row the way pre-0022 code did (no family_id): it is
    its own family, and its successor joins it."""
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 200, resp.text
    rows = await _rows(factory, user.id)
    root, child = rows[_jti(refresh)], rows[_jti(resp.json()["refresh_token"])]
    assert root.family_id is None
    assert child.family_id == root.id
    assert root.replaced_by_id == child.id
    assert root.used_at is not None and root.revoked_at is not None


async def _race_n(factory, client, token: str, followers: int):
    """A presents ``token`` and pauses right after loading the user row
    (holding its lock); ``followers`` more requests present the same token,
    read the refresh row as live and wait for the lock. Returns (A's
    response, the followers' responses, whether they all had to wait)."""
    paused = async_sessionmaker(
        factory.kw["bind"], class_=_PausingSession, expire_on_commit=False
    )
    others: list[asyncio.Task] = []
    waited: list[bool] = []

    async def interleave():
        _PausingSession.after_user_load = None
        others.extend(
            asyncio.create_task(
                client.post("/auth/refresh", json={"refresh_token": token})
            )
            for _ in range(followers)
        )
        done, _ = await asyncio.wait(others, timeout=1)
        waited.append(not done)

    async def _paused_db():
        async with paused() as session:
            yield session

    plain_db = app.dependency_overrides[get_db]
    _PausingSession.after_user_load = interleave
    app.dependency_overrides[get_db] = _paused_db
    try:
        resp_a = await client.post("/auth/refresh", json={"refresh_token": token})
        rest = await asyncio.wait_for(asyncio.gather(*others), timeout=10)
    finally:
        _PausingSession.after_user_load = None
        app.dependency_overrides[get_db] = plain_db
    return resp_a, list(rest), waited == [True]


async def _race(factory, client, token: str):
    """A and one follower B (see ``_race_n``). Returns (A's response, B's
    response, whether B had to wait)."""
    resp_a, (resp_b,), waited = await _race_n(factory, client, token, 1)
    return resp_a, resp_b, waited


async def test_concurrent_refreshes_rotate_once_and_are_not_reuse(env):
    """As test_token_version_db's fork test, plus: the second request is a
    race, not a replay, so nothing is revoked, token_version is unchanged,
    and no alert is sent. It gets a fresh pair and displaces the first one's
    (#734 review S1), so one session stays live."""
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    with captured_app_events() as events:
        resp_a, resp_b, b_waited = await _race(factory, client, refresh)

    assert b_waited
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text
    assert _warnings(events) == []

    rows = await _rows(factory, user.id)
    assert len(rows) == 3
    root = rows[_jti(refresh)]
    a_row = rows[_jti(resp_a.json()["refresh_token"])]
    b_row = rows[_jti(resp_b.json()["refresh_token"])]
    assert [r for r in rows.values() if r.revoked_at is None] == [b_row]
    assert root.replaced_by_id == a_row.id
    assert a_row.displaced_at is not None and a_row.replaced_by_id == b_row.id
    assert b_row.family_id == a_row.family_id == root.id
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 0
    # Both access tokens work until they expire (nothing is revoked); B's
    # refresh token is the one that continues the session.
    for resp in (resp_a, resp_b):
        me = await client.get("/auth/me", headers=_auth(resp.json()["access_token"]))
        assert me.status_code == 200
    nxt = await client.post(
        "/auth/refresh", json={"refresh_token": resp_b.json()["refresh_token"]}
    )
    assert nxt.status_code == 200


@pytest.mark.parametrize("thief_first", [True, False])
async def test_thief_who_wins_the_race_is_caught(env, thief_first):
    """#734 review S1: a thief presents a stolen live token and wins the user
    lock; the real client's in-flight refresh with the same token used to get
    a plain 401 (the app signs out and never presents it again) while the
    thief kept rotating undetected. Now the victim gets a replacement and the
    thief's pair is displaced: when the victim and the thief each refresh
    once more, reuse fires and everything is revoked, in either order."""
    factory, client, make_user, _ = env
    user, access, t0 = await make_user()
    with captured_app_events() as events:
        thief, victim, _ = await _race(factory, client, t0)
    assert thief.status_code == victim.status_code == 200
    assert _warnings(events) == []

    thief_tokens, victim_tokens = thief.json(), victim.json()
    with captured_app_events() as events:
        if thief_first:
            t = await client.post(
                "/auth/refresh", json={"refresh_token": thief_tokens["refresh_token"]}
            )
            v = await client.post(
                "/auth/refresh", json={"refresh_token": victim_tokens["refresh_token"]}
            )
            assert t.json()["detail"] == "refresh_reused"
            assert v.status_code == 401
        else:
            v = await client.post(
                "/auth/refresh", json={"refresh_token": victim_tokens["refresh_token"]}
            )
            assert v.status_code == 200
            t = await client.post(
                "/auth/refresh", json={"refresh_token": thief_tokens["refresh_token"]}
            )
            assert t.json()["detail"] == "refresh_reused"
            victim_tokens = v.json()
    (alert,) = _warnings(events)
    assert alert["tags"]["family_id"] == str(
        (await _rows(factory, user.id))[_jti(t0)].id
    )

    rows = await _rows(factory, user.id)
    assert rows and all(r.revoked_at is not None for r in rows.values())
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
    for token in (access, thief_tokens["access_token"], victim_tokens["access_token"]):
        me = await client.get("/auth/me", headers=_auth(token))
        assert me.status_code == 401


async def test_three_requests_with_one_live_token_are_reuse(env):
    """#734 review S3: a thief sends two refreshes with a stolen live token
    while the real client's refresh waits behind them. The first rotates it,
    the second (a race) displaces that successor, and the third finds its
    twin's successor displaced: one app never sends three, so it is reuse,
    not the plain 401 that let the thief keep the session unnoticed."""
    factory, client, make_user, _ = env
    user, _access, t0 = await make_user()
    with captured_app_events() as events:
        first, rest, waited = await _race_n(factory, client, t0, 2)

    assert waited
    assert first.status_code == 200, first.text
    statuses = sorted(r.status_code for r in rest)
    assert statuses == [200, 401], [r.text for r in rest]
    (reused,) = [r for r in rest if r.status_code == 401]
    assert reused.json()["detail"] == "refresh_reused"
    assert len(_warnings(events)) == 1

    rows = await _rows(factory, user.id)
    assert rows and all(r.revoked_at is not None for r in rows.values())
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
    for resp in [first, *rest]:
        if resp.status_code == 200:
            nxt = await client.post(
                "/auth/refresh",
                json={"refresh_token": resp.json()["refresh_token"]},
            )
            assert nxt.status_code == 401


async def test_grace_window_retry_replaces_successor(env):
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    lost = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert lost.status_code == 200

    retry = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert retry.status_code == 200, retry.text
    new = retry.json()

    rows = await _rows(factory, user.id)
    root = rows[_jti(refresh)]
    lost_row = rows[_jti(lost.json()["refresh_token"])]
    new_row = rows[_jti(new["refresh_token"])]
    assert root.replaced_by_id == lost_row.id
    assert lost_row.replaced_by_id == new_row.id
    assert lost_row.used_at is not None and lost_row.revoked_at is not None
    assert lost_row.displaced_at is not None and new_row.displaced_at is None
    assert new_row.revoked_at is None
    assert new_row.family_id == lost_row.family_id == root.id
    assert [r for r in rows.values() if r.revoked_at is None] == [new_row]

    me = await client.get("/auth/me", headers=_auth(new["access_token"]))
    assert me.status_code == 200
    again = await client.post(
        "/auth/refresh", json={"refresh_token": new["refresh_token"]}
    )
    assert again.status_code == 200


async def test_displaced_token_inside_window_is_reuse(env):
    """#734 review (Codex P1 / S2): a stolen token A replayed inside the
    window displaces the real client's B with C. The real client then
    presents B, still inside the window: reuse and an alert, not another
    retry, so the two holders cannot alternate."""
    factory, client, make_user, _ = env
    user, _access, a = await make_user()
    b = await client.post("/auth/refresh", json={"refresh_token": a})
    c = await client.post("/auth/refresh", json={"refresh_token": a})
    assert b.status_code == c.status_code == 200

    with captured_app_events() as events:
        back = await client.post(
            "/auth/refresh", json={"refresh_token": b.json()["refresh_token"]}
        )
    assert back.status_code == 401
    assert back.json()["detail"] == "refresh_reused"
    assert len(_warnings(events)) == 1
    rows = await _rows(factory, user.id)
    assert all(r.revoked_at is not None for r in rows.values())
    me = await client.get("/auth/me", headers=_auth(c.json()["access_token"]))
    assert me.status_code == 401


async def test_parallel_replays_get_one_retry_then_reuse(env):
    """#734 review: four parallel replays of a just-rotated token each got a
    retry. Now one gets a pair and the rest find a displaced successor:
    reuse, one alert, nothing left live."""
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    first = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert first.status_code == 200
    with captured_app_events() as events:
        resps = await asyncio.gather(
            *[
                client.post("/auth/refresh", json={"refresh_token": refresh})
                for _ in range(4)
            ]
        )
    codes = sorted(r.status_code for r in resps)
    details = [r.json().get("detail") for r in resps if r.status_code == 401]
    assert codes == [200, 401, 401, 401]
    assert details.count("refresh_reused") == 1
    assert len(_warnings(events)) == 1
    rows = await _rows(factory, user.id)
    assert all(r.revoked_at is not None for r in rows.values())


async def test_second_retry_is_reuse(env):
    factory, client, make_user, _ = env
    user, _access, refresh = await make_user()
    for _ in range(2):
        ok = await client.post("/auth/refresh", json={"refresh_token": refresh})
        assert ok.status_code == 200
    with captured_app_events() as events:
        again = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert again.json()["detail"] == "refresh_reused"
    assert len(_warnings(events)) == 1
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1


async def test_reuse_after_window_revokes_everything_and_alerts(env):
    factory, client, make_user, _ = env
    user, access, refresh = await make_user()
    rotated = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert rotated.status_code == 200
    window = settings.refresh_reuse_grace_seconds
    await _backdate(factory, _jti(refresh), window + 1)

    with captured_app_events() as events:
        resp = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "refresh_reused"

    rows = await _rows(factory, user.id)
    assert len(rows) == 2
    assert all(r.revoked_at is not None for r in rows.values())
    async with factory() as db:
        assert (await db.get(User, user.id)).token_version == 1
    for token in (access, rotated.json()["access_token"]):
        me = await client.get("/auth/me", headers=_auth(token))
        assert me.status_code == 401
        assert me.json()["detail"] == "token_revoked"

    (alert,) = _warnings(events)
    assert alert["tags"]["user_id"] == str(user.id)
    assert alert["tags"]["family_id"] == str(rows[_jti(refresh)].id)
    text = str(alert)
    for secret in (refresh, rotated.json()["refresh_token"], *rows, user.email):
        assert secret not in text

    # A second replay is not a second alert: everything is already revoked.
    with captured_app_events() as events:
        again = await client.post("/auth/refresh", json={"refresh_token": refresh})
    assert again.json()["detail"] == "token_revoked"
    assert _warnings(events) == []


async def test_purge_keeps_rotated_rows_and_nulls_pointers(env):
    factory, _client, make_user, _ = env
    user, _access, _refresh = await make_user()
    now = datetime.now(UTC)
    later = now + timedelta(days=1)

    def row(tag, **kw):
        return RefreshToken(
            id=uuid.uuid4(),
            jti=f"{user.id}-{tag}",
            user_id=user.id,
            expires_at=kw.pop("expires_at", later),
            **kw,
        )

    logged_out = row("logged-out", revoked_at=now)
    rotated = row("rotated", revoked_at=now, used_at=now)
    expired_rotated = row(
        "expired", revoked_at=now, used_at=now, expires_at=now - timedelta(seconds=1)
    )
    live = row("live")
    async with factory() as db:
        db.add_all([logged_out, live, expired_rotated])
        await db.flush()
        rotated.replaced_by_id = logged_out.id
        db.add(rotated)
        await db.commit()

    with patch.object(refresh_family, "PURGE_BATCH_SIZE", 1):
        async with factory() as db:
            deleted = await refresh_family.purge_refresh_tokens(db)
    assert deleted >= 2  # other users' purgeable rows may go too

    rows = await _rows(factory, user.id)
    kept = {jti.removeprefix(f"{user.id}-") for jti in rows}
    # make_user's own live row stays as well.
    assert {"rotated", "live"} <= kept
    assert "logged-out" not in kept and "expired" not in kept
    assert rows[rotated.jti].replaced_by_id is None


async def test_purge_skips_a_row_whose_predecessor_a_replay_holds(env):
    """#734 review C1: a replay of P holds P's row lock and then locks its
    successor X; the purge deleting X nulls P.replaced_by_id, which needs P's
    lock. Before, the two deadlocked (40P01). Now the purge skips X without
    waiting, and deletes it on a later run once P is free."""
    factory, client, make_user, _ = env
    user, _access, t0 = await make_user()
    t1 = (await client.post("/auth/refresh", json={"refresh_token": t0})).json()
    out = await client.post("/auth/logout", json={"refresh_token": t1["refresh_token"]})
    assert out.status_code == 204  # X is revoked without rotation: purgeable
    rows = await _rows(factory, user.id)
    p, x = rows[_jti(t0)], rows[_jti(t1["refresh_token"])]
    assert p.replaced_by_id == x.id

    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as replay:
            await replay.begin()
            await replay.execute(
                select(RefreshToken.id).where(RefreshToken.id == p.id).with_for_update()
            )
            async with factory() as db:
                await asyncio.wait_for(
                    refresh_family.purge_refresh_tokens(db), timeout=10
                )
            # The replay can still lock X (the purge left it alone).
            await asyncio.wait_for(
                replay.execute(
                    select(RefreshToken.id)
                    .where(RefreshToken.id == x.id)
                    .with_for_update()
                ),
                timeout=5,
            )
            await replay.rollback()
    finally:
        await engine.dispose()
    rows = await _rows(factory, user.id)
    assert x.jti in rows and rows[p.jti].replaced_by_id == x.id

    # P is free again: the next run deletes X and nulls the pointer.
    async with factory() as db:
        await refresh_family.purge_refresh_tokens(db)
    rows = await _rows(factory, user.id)
    assert x.jti not in rows
    assert rows[p.jti].replaced_by_id is None


async def test_purge_skips_a_locked_row(env):
    """A row another transaction holds (a replay locking the successor) is
    skipped, not waited for."""
    factory, client, make_user, _ = env
    user, _access, t0 = await make_user()
    t1 = (await client.post("/auth/refresh", json={"refresh_token": t0})).json()
    await client.post("/auth/logout", json={"refresh_token": t1["refresh_token"]})
    x_jti = _jti(t1["refresh_token"])

    engine = create_async_engine(settings.async_database_url)
    try:
        async with engine.connect() as other:
            await other.begin()
            await other.execute(
                select(RefreshToken.id)
                .where(RefreshToken.jti == x_jti)
                .with_for_update()
            )
            async with factory() as db:
                await asyncio.wait_for(
                    refresh_family.purge_refresh_tokens(db), timeout=10
                )
            await other.rollback()
    finally:
        await engine.dispose()
    assert x_jti in await _rows(factory, user.id)
