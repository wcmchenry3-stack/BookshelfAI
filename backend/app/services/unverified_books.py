"""UnverifiedBookService — the user's own entries for books the catalog
cannot find (#616).

An unverified entry is a ``user_custom_books`` row (title, author, an
optional ISBN-13 the user typed) and the ``user_books`` row that holds it
(``custom_book_id``, status ``wishlisted``). Both belong to the requesting
user only. Nothing here reads or writes ``books`` or ``editions``: the
shared catalog stays provider-only (#602), an entry's ISBN never claims
``editions.isbn_13``, and no catalog lookup or library check
(``deduplication.held_entries``, which joins ``books``) can ever match an
entry, whoever's it is. So the user's own unverified entry never counts as
"already owned" for a scan or search result, and never blocks or changes
adding a real catalog book.

Refusals (nothing written):

- :class:`UnverifiedDuplicateError` — the user already has an unverified
  entry with the same title and author (casefolded, punctuation and
  whitespace normalised) or the same ISBN-13.
- :class:`UnverifiedLimitError` — the user already holds
  ``settings.unverified_books_per_user_max`` unverified entries.

Only live entries count for both: an entry in the trash (#683) is neither a
duplicate nor counted towards the cap. Restoring one from the trash runs the
same checks (``POST /user-books/{id}/restore``).

The check and the insert run with the user's ``users`` row locked
(``SELECT ... FOR NO KEY UPDATE``), so concurrent requests from one user
are serialised and can neither exceed the cap nor add the same book twice.
That lock conflicts with itself, which is all that is needed, but not with
the ``FOR KEY SHARE`` lock that an insert into a table referencing ``users``
takes, so it does not block those inserts the way ``FOR UPDATE`` would.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook
from app.schemas.user_book import UnverifiedBookCreate
from app.services.enrichment import normalise_text

logger = logging.getLogger(__name__)


class UnverifiedDuplicateError(Exception):
    """The user already has this unverified entry (409
    ``unverified_duplicate``)."""


class UnverifiedLimitError(Exception):
    """The user is at the cap on unverified entries (409
    ``unverified_limit_reached``)."""


def _part(value: str) -> str:
    """One half of the duplicate key: the normalised text, or, when that is
    empty (a title or author of only punctuation), the stripped, casefolded
    text itself, so "!!!" and "???" are not the same book as "..." and "—"."""
    return normalise_text(value) or value.strip().casefold()


def _key(title: str, author: str) -> tuple[str, str]:
    return _part(title), _part(author)


async def lock_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Serialise this user's unverified adds and restores (#683): the cap
    and the duplicate check read the user's entries, and two concurrent
    requests must not both pass."""
    await db.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True)
    )


async def refuse_if_cannot_hold(
    db: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    author: str,
    isbn: str | None,
) -> None:
    """:class:`UnverifiedDuplicateError` or :class:`UnverifiedLimitError`
    if the user cannot hold one more unverified entry with this title,
    author and ISBN. Only live entries count: an entry in the trash (#683)
    neither blocks a duplicate nor counts towards the cap. Call with the
    user locked (:func:`lock_user`). Writes nothing."""
    result = await db.execute(
        select(
            UserCustomBook.title, UserCustomBook.author, UserCustomBook.isbn_13
        ).where(UserCustomBook.user_id == user_id, UserCustomBook.live())
    )
    existing = result.all()

    key = _key(title, author)
    if any(
        _key(row.title, row.author) == key or (isbn is not None and row.isbn_13 == isbn)
        for row in existing
    ):
        raise UnverifiedDuplicateError
    if len(existing) >= settings.unverified_books_per_user_max:
        raise UnverifiedLimitError


class UnverifiedBookService:
    async def add(
        self, db: AsyncSession, user_id: uuid.UUID, req: UnverifiedBookCreate
    ) -> UserBook:
        await lock_user(db, user_id)
        try:
            await refuse_if_cannot_hold(db, user_id, req.title, req.author, req.isbn)
        except UnverifiedDuplicateError:
            await db.rollback()
            logger.info("unverified: duplicate of the user's own entry")
            raise
        except UnverifiedLimitError:
            await db.rollback()
            logger.info("unverified: per-user cap reached")
            raise

        custom = UserCustomBook(
            user_id=user_id, title=req.title, author=req.author, isbn_13=req.isbn
        )
        db.add(custom)
        await db.flush()
        user_book = UserBook(
            user_id=user_id,
            custom_book_id=custom.id,
            status="wishlisted",
            wishlisted_at=datetime.now(UTC),
        )
        db.add(user_book)
        await db.commit()
        await db.refresh(user_book, ["custom_book"])
        return user_book
