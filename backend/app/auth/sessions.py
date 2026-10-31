"""Issuing (#844) and revoking (#689) a user's sessions."""

from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.auth.jwt import (
    access_token_ttl_seconds,
    create_access_token,
    create_refresh_token,
)
from app.auth.refresh_family import new_family_row
from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User


async def revoke_all_sessions(db: AsyncSession, user: User) -> None:
    """Sign the user out everywhere: bump ``users.token_version`` and revoke
    every refresh token they hold.

    The bump makes every access token issued before it fail
    ``get_current_user`` with 401 ``token_revoked`` on its next request; the
    revoked rows (and the ``tv`` claim in refresh tokens) stop any refresh
    token from minting new ones. The increment runs in SQL, so two concurrent
    calls both count; ``user.token_version`` is set from the bumped row
    (``RETURNING``) without marking it dirty, so a later flush cannot write
    a stale value back.

    Does not commit: the caller commits, together with its own changes.
    Callers: ``POST /auth/logout-all`` and refresh-token reuse detection in
    ``POST /auth/refresh`` (#690). Account deletion (#291) is to call it too.
    """
    now = datetime.now(UTC)
    bumped = await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(token_version=User.token_version + 1)
        .returning(User.token_version)
        .execution_options(synchronize_session=False)
    )
    set_committed_value(user, "token_version", bumped.scalar_one())
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


class IssuedTokens(NamedTuple):
    """An access/refresh token pair as returned to the client."""

    access_token: str
    refresh_token: str
    expires_in: int


async def issue_session_tokens(db: AsyncSession, user: User) -> IssuedTokens:
    """Mint an access + refresh token pair for ``user`` and start a new
    refresh-token family (#844).

    Adds the family's first ``RefreshToken`` row and commits, so the caller's
    own pending changes are committed too. Shared by ``POST /auth/google`` and
    the e2e ``POST /auth/test-login`` route; ``POST /auth/refresh`` rotates an
    existing family instead and does not use this.
    """
    access_token = create_access_token(str(user.id), user.email, user.token_version)
    refresh_token, jti = create_refresh_token(str(user.id), user.token_version)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expiry_days)
    db.add(new_family_row(user.id, jti, expires_at))
    await db.commit()
    return IssuedTokens(access_token, refresh_token, access_token_ttl_seconds())
