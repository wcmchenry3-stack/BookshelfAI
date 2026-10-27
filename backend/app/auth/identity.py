"""Find or create the user behind a verified sign-in (#688).

Identity is the provider's stable subject (Google's ``sub``, stored in
``users.google_id``), never the email address: a Google account can change
its address, and an address can later belong to a different account. The
email is profile data that is refreshed from the verified token.

Email comparisons ignore case (``lower(email)``): ``Alice@x.com`` and
``alice@x.com`` are the same address for conflict purposes. Emails are stored
as Google reports them.

Logs here carry user ids only, never an email address.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

logger = logging.getLogger(__name__)

# The HTTP ``detail`` returned when a sign-in collides with another account.
ACCOUNT_CONFLICT = "account_conflict"


def email_allowed(email: str, allowlist: list[str]) -> bool:
    """``ALLOWED_EMAILS`` check, ignoring case. An empty list allows all."""
    if not allowlist:
        return True
    return email.lower() in {e.lower() for e in allowlist}


def _by_email(email: str, exclude_id=None):
    """The oldest user holding ``email``, ignoring case, other than
    ``exclude_id``. ``limit(1)`` so rows that already differ only by case can
    never raise MultipleResultsFound."""
    query = select(User).where(func.lower(User.email) == email.lower())
    if exclude_id is not None:
        query = query.where(User.id != exclude_id)
    return query.order_by(User.created_at, User.id).limit(1)


_UNIQUE_VIOLATION = "23505"


def is_unique_violation(exc: IntegrityError) -> bool:
    """True only for SQLSTATE 23505. SQLAlchemy's asyncpg adapter exposes
    ``sqlstate``/``pgcode`` on ``exc.orig``; the raw asyncpg error is its
    ``__cause__``. NOT NULL, FK and check violations are not races."""
    orig = exc.orig
    for err in (orig, getattr(orig, "__cause__", None)):
        if err is None:
            continue
        for attr in ("sqlstate", "pgcode"):
            if getattr(err, attr, None) == _UNIQUE_VIOLATION:
                return True
    return False


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
      stored too, unless another user already holds that address, ignoring
      case (rows differing only by case can exist); then the old address is kept and the
      sign-in still succeeds, because the subject proves who this is.
    - Unknown ``google_id`` but the email (ignoring case) belongs to an
      existing user: that user necessarily has a different ``google_id`` (it
      is unique and not null), so this raises :class:`AccountConflictError`.
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
            # Also for a case-only change: another row may hold this exact
            # spelling (only `email` is unique, case-sensitively).
            holder = (
                await db.execute(_by_email(email, exclude_id=user.id))
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

    holder = (await db.execute(_by_email(email))).scalar_one_or_none()
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
        token_version=0,
    )
    db.add(user)
    return user


async def resolve_and_commit_google_user(
    db: AsyncSession,
    *,
    google_id: str,
    email: str,
    display_name: str | None,
    avatar_url: str | None,
) -> User:
    """:func:`resolve_google_user`, then commit.

    A unique violation at commit means a concurrent sign-in won the race
    (a double tap with the same new subject, or another account taking the
    email first). The session is rolled back and the lookup runs once more,
    which then finds the winner's row: the same subject signs in, a
    different one gets :class:`AccountConflictError`. A second violation is
    also reported as a conflict. Any other integrity error (NOT NULL, FK)
    is a bug, not a race: it is rolled back and re-raised.
    """
    for attempt in (1, 2):
        user = await resolve_google_user(
            db,
            google_id=google_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            if not is_unique_violation(exc):
                raise
            logger.warning("Sign-in hit a unique constraint (attempt %d of 2)", attempt)
            continue
        await db.refresh(user)
        return user
    raise AccountConflictError
