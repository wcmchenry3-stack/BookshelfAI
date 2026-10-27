"""Revoking every session a user holds (#689)."""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

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
    Callers: ``POST /auth/logout-all``. Account deletion (#291) and refresh
    reuse detection (#690) are to call it too.
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
