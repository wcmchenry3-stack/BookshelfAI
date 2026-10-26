"""Database and credential setup for the /scan load run (#539).

DANGER: this wipes data. For the database named by DATABASE_URL it deletes

- EVERY row of ``ai_settings``, ``ai_quota``, ``ai_inflight``,
  ``ai_spend_daily``, ``ai_usage``, ``ai_usage_daily``,
  ``ai_enhanced_zero_results``, ``scan_records`` and ``scan_usage_daily``
  (``ai_settings`` included: that is every operator dial and kill switch, and
  why the database must be throwaway);
- the users whose email ends in ``@perf.invalid`` and only THEIR
  ``refresh_tokens``;

then creates new users. It therefore refuses to run unless DATABASE_URL

- parses (SQLAlchemy ``make_url``) to a LOCAL host (localhost, 127.0.0.1,
  ::1) and has NO query parameter at all (the driver lets ``?database=``,
  ``?host=`` and the like override what was parsed),
- names a database whose name contains ``perf`` (the workflow uses ``perf``,
  a local run e.g. ``bs539_perf``), and
- comes with ``PERF_CONFIRM_ISOLATED_DB=yes``.

Then, before any delete, in the same session that deletes, the server is asked
which database this session really reached (:func:`verify_connected_database`):
``current_database()`` must contain ``perf``. The server address is not
checked: in the workflow Postgres runs in a service container reached through
a published port, so the server sees its own bridge address, not loopback.
The local-host URL and the no-query-parameter rule above cover where we
connect; this check covers which database the driver actually opened.

The app modules are imported inside the functions, after the caller has put
the throwaway JWT key pair and the other settings in ``os.environ``.
"""

from __future__ import annotations

import os
import uuid

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.engine import make_url

EMAIL_DOMAIN = "perf.invalid"
LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUIRED_DB_NAME_PART = "perf"


class UnsafeDatabaseError(RuntimeError):
    pass


def check_database_url(url: str, confirmed: str | None) -> None:
    """Raise :class:`UnsafeDatabaseError` unless ``url`` is a local database
    named ``*perf*`` and ``confirmed`` is ``yes``. Messages never include the
    URL (it may hold a password)."""
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - any parse failure is a refusal
        raise UnsafeDatabaseError("DATABASE_URL cannot be parsed") from None
    if parsed.host not in LOCAL_DB_HOSTS:
        raise UnsafeDatabaseError("DATABASE_URL must name a local host")
    # An allowlist of nothing: the run needs no query parameter, and the
    # driver lets any of them override the parsed host, port, user or
    # database (asyncpg: ?database=, ?host=, ...).
    if parsed.query:
        raise UnsafeDatabaseError(
            "DATABASE_URL must have no query parameters "
            f"(found: {', '.join(sorted(parsed.query))}): the driver lets them "
            "override the host and database"
        )
    if REQUIRED_DB_NAME_PART not in (parsed.database or "").lower():
        raise UnsafeDatabaseError(
            f"the database name must contain {REQUIRED_DB_NAME_PART!r} "
            "(a throwaway database made for this run)"
        )
    if confirmed != "yes":
        raise UnsafeDatabaseError(
            "set PERF_CONFIRM_ISOLATED_DB=yes to confirm DATABASE_URL is a "
            "throwaway database: the load run deletes every ai_settings, AI "
            "quota, spend, usage and scan-record row"
        )


def check_server_identity(database: str | None) -> None:
    """Refuse unless the database this session actually reached is named
    ``*perf*`` (whatever the URL said)."""
    if REQUIRED_DB_NAME_PART not in (database or "").lower():
        raise UnsafeDatabaseError(
            "the connected database is not a perf database: refusing to delete"
        )


async def verify_connected_database(db) -> None:
    """Ask the server which database THIS session reached, before anything is
    deleted. Defence in depth behind :func:`check_database_url`."""
    from sqlalchemy import func, select

    database = (await db.execute(select(func.current_database()))).scalar_one()
    check_server_identity(database)


def require_isolated_db() -> None:
    check_database_url(
        os.environ.get("DATABASE_URL", ""), os.environ.get("PERF_CONFIRM_ISOLATED_DB")
    )


def generate_jwt_keys() -> tuple[str, str]:
    """A throwaway RS256 key pair (PEM strings), never stored anywhere."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


async def _reset_and_create_users(count: int) -> list[str]:
    """Clear the previous scenario's state, create ``count`` Free users and
    return one access token for each (minted with the app's own
    ``create_access_token`` and the throwaway key)."""
    require_isolated_db()
    from sqlalchemy import delete, select

    from app.auth.jwt import create_access_token
    from app.core import database
    from app.models import (
        AIEnhancedZeroResult,
        AIInflight,
        AIQuota,
        AISetting,
        AISpendDaily,
        AIUsage,
        AIUsageDaily,
        RefreshToken,
        ScanRecord,
        ScanUsageDaily,
        User,
    )

    tokens = []
    async with database.AsyncSessionLocal() as db:
        await verify_connected_database(db)
        for model in (
            AIEnhancedZeroResult,
            AIInflight,
            AIQuota,
            AISetting,
            AISpendDaily,
            AIUsage,
            AIUsageDaily,
            ScanRecord,
            ScanUsageDaily,
        ):
            await db.execute(delete(model))
        # Only the perf users' tokens; the users themselves go next.
        perf_users = select(User.id).where(User.email.like(f"%@{EMAIL_DOMAIN}"))
        await db.execute(
            delete(RefreshToken).where(RefreshToken.user_id.in_(perf_users))
        )
        await db.execute(delete(User).where(User.email.like(f"%@{EMAIL_DOMAIN}")))
        for _ in range(count):
            user_id = uuid.uuid4()
            email = f"perf-{user_id.hex[:12]}@{EMAIL_DOMAIN}"
            db.add(User(id=user_id, email=email, google_id=f"perf-{user_id.hex}"))
            tokens.append(create_access_token(str(user_id), email))
        await db.commit()
    return tokens


async def _apply_settings(values: dict, provider: str) -> None:
    """Write ``ai_settings`` rows with the app's own validated writers and
    point ``scan.standard`` at ``provider`` (the fake speaks Anthropic's API)."""
    require_isolated_db()
    from app.ai import settings as ai_settings
    from app.core import database

    async with database.AsyncSessionLocal() as db:
        await verify_connected_database(db)
        await ai_settings.switch_provider(
            db, "scan.standard", provider, updated_by="perf"
        )
        for key, value in values.items():
            await ai_settings.set_setting(db, key, value, updated_by="perf")


async def prepare(
    count: int, values: dict, *, provider: str = "anthropic"
) -> list[str]:
    """Reset the database, create ``count`` users, write the scenario's
    settings; return the users' tokens. One event loop for all of it, and the
    engine is disposed after."""
    from app.core import database

    try:
        tokens = await _reset_and_create_users(count)
        await _apply_settings(values, provider)
        return tokens
    finally:
        await database.engine.dispose()
