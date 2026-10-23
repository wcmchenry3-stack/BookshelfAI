"""Database and credential setup for the /scan load run (#539).

Everything here writes to the DATABASE_URL database, so it refuses to run
unless that database is on this machine AND ``PERF_CONFIRM_ISOLATED_DB=yes``
says it is a throwaway one (the workflow's own Postgres service, or a scratch
database for a local run). It deletes every row of the AI quota, spend, usage,
scan-record and settings tables and of the perf users, then creates new users.

The app modules are imported inside the functions, after the caller has put
the throwaway JWT key pair and the other settings in ``os.environ``.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

EMAIL_DOMAIN = "perf.invalid"
LOCAL_DB_HOSTS = {"localhost", "127.0.0.1", "::1"}


class UnsafeDatabaseError(RuntimeError):
    pass


def require_isolated_db() -> None:
    url = os.environ.get("DATABASE_URL", "")
    host = urlsplit(url).hostname
    if host not in LOCAL_DB_HOSTS:
        raise UnsafeDatabaseError("DATABASE_URL must point at a local database")
    if os.environ.get("PERF_CONFIRM_ISOLATED_DB") != "yes":
        raise UnsafeDatabaseError(
            "set PERF_CONFIRM_ISOLATED_DB=yes to confirm DATABASE_URL is a "
            "throwaway database: the load run deletes AI quota, spend, usage, "
            "scan-record and settings rows"
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
    from sqlalchemy import delete

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
        for model in (
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
        ):
            await db.execute(delete(model))
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
