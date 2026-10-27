"""Find or create the user behind a verified sign-in (#688).

Identity is the provider's stable subject (Google's ``sub``, stored in
``users.google_id``), never the email address: a Google account can change
its address, and an address can later belong to a different account. The
email is profile data that is refreshed from the verified token.

Logs here carry user ids only, never an email address.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

logger = logging.getLogger(__name__)

# The HTTP ``detail`` returned when a sign-in collides with another account.
ACCOUNT_CONFLICT = "account_conflict"


class AccountConflictError(Exception):
    """The verified email belongs to a user with a different provider subject.

    Never merged silently: the caller answers 409 ``account_conflict``.
    """


async def resolve_google_user(
    db: AsyncSession,
    *,
    google_id: str,
    email: str,
    display_name: str | None,
    avatar_url: str | None,
) -> User:
    """Return the user for ``google_id``, creating one when it is new.

    - Known ``google_id``: profile fields are refreshed. A changed email is
      stored too, unless another user already holds that address; then the
      old address is kept (``users.email`` is unique) and the sign-in still
      succeeds, because the subject proves who this is.
    - Unknown ``google_id`` but the email belongs to an existing user: that
      user necessarily has a different ``google_id`` (it is unique and not
      null), so this raises :class:`AccountConflictError`.
    - Otherwise a new user is added to the session (not committed).
    """
    if not google_id:
        raise ValueError("google_id is required")

    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()
    if user is not None:
        user.display_name = display_name
        user.avatar_url = avatar_url
        if email and user.email != email:
            holder = (
                await db.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if holder is None:
                user.email = email
                logger.info("Sign-in email updated (user_id=%s)", user.id)
            else:
                logger.warning(
                    "Sign-in email already held by another user; keeping the "
                    "stored email (user_id=%s, holder_user_id=%s)",
                    user.id,
                    holder.id,
                )
        return user

    result = await db.execute(select(User).where(User.email == email))
    holder = result.scalar_one_or_none()
    if holder is not None:
        logger.warning(
            "Sign-in rejected: email belongs to a user with a different "
            "subject (holder_user_id=%s)",
            holder.id,
        )
        raise AccountConflictError

    user = User(
        email=email,
        google_id=google_id,
        display_name=display_name,
        avatar_url=avatar_url,
    )
    db.add(user)
    return user
