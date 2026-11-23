"""The ``Idempotency-Key`` store (#685): the only code that writes
``idempotency_keys``.

A keyed write goes through these steps, all on the request's own session
(``app/api/_idempotency.py`` drives them):

1. :func:`claim`, before the endpoint runs: ``INSERT ... ON CONFLICT DO
   NOTHING`` of a ``pending`` row on ``(user_id, key)``, committed at once.
   The unique constraint makes the insert the lock: of two concurrent
   requests with the same key exactly one inserts, and the other (its insert
   waits for the first insert's commit, a few milliseconds) reads the row and
   is refused or replayed, so the work runs once. Committing the claim on
   its own, before the work, rather than in the work's transaction: the
   endpoints commit and roll back themselves (some refuse with a rollback),
   which would take an uncommitted claim with them, and a duplicate would
   wait on the row lock for the whole request instead of hearing ``409``.
2. :func:`arm_commit_marker`: a ``before_commit`` listener on the request's
   session turns the row ``pending`` -> ``committed`` **inside the
   endpoint's own transaction**, so the row says "the write happened"
   exactly when the write is durable, whatever fails afterwards (a timeout
   in a later read, the worker dying before step 3). A ``committed`` row is
   never released and never taken over. If the row is no longer this
   request's ``pending`` row (a retry took it over after
   :data:`PENDING_LEASE`), the listener raises :class:`KeyLostError` and
   the endpoint's commit fails, so the old request can never apply its
   write after the new one.
3. :func:`complete`: stores the response (status, body, ETag / Location)
   on the row (``complete``), so a retry replays it.
4. :func:`release`: deletes the row after a 5xx or an exception, but only
   while it is still ``pending`` (nothing committed), so a retry runs the
   request again instead of replaying a failure.

What a repeat with the same key and request gets: ``complete`` -> the stored
response; ``pending`` -> :class:`KeyInProgressError` (409); ``committed`` ->
:class:`KeyOutcomeUnknownError` (409 ``idempotency_outcome_unknown``): the
write was applied but its response was never stored (the request failed
after its commit, or is between its commit and step 3), so it must not run
again and there is nothing to replay.

A row older than :data:`KEY_TTL` (24 h) is never replayed or refused:
:func:`claim` deletes it and inserts afresh. So is a ``pending`` row older
than :data:`PENDING_LEASE`, which most likely belongs to a request that died
before committing. Nothing bounds a whole request's duration, so the old
request could in principle still be running; step 2 makes that safe, since
its commit then fails. The daily cleanup (``app/services/token_cleanup.py``)
deletes rows past :data:`KEY_TTL` in batches (:func:`purge_expired`), so a
row lasts at most about 48 hours.

Never logs a key or a body.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import and_, delete, event, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.idempotency_key import (
    STATE_COMMITTED,
    STATE_COMPLETE,
    STATE_PENDING,
    IdempotencyKey,
)

logger = logging.getLogger(__name__)

KEY_TTL: Final = timedelta(hours=24)
PENDING_LEASE: Final = timedelta(minutes=2)
# Response headers kept for a replay; every other header is the server's
# own (request id, security headers) and is set afresh on the replay.
REPLAYED_HEADERS: Final = ("etag", "location")

PURGE_BATCH_SIZE: Final = 1_000
PURGE_MAX_BATCHES: Final = 200

# Insert, find the row gone (released or purged meanwhile) or stale, retry.
_CLAIM_ATTEMPTS: Final = 3


class KeyReusedError(Exception):
    """The key was used for a different request (method, path, If-Match or
    body) within :data:`KEY_TTL`."""


class KeyInProgressError(Exception):
    """The first request with this key is still running."""


class KeyOutcomeUnknownError(Exception):
    """The first request with this key committed its write, but no response
    was stored: it must not run again, and there is nothing to replay."""


class KeyLostError(Exception):
    """Raised from the commit of a request whose pending row was taken over
    by a retry: its write must not be applied."""


@dataclass(frozen=True)
class StoredResponse:
    """What a replay sends back: the first response's status, JSON body
    (``None`` for a 204) and its :data:`REPLAYED_HEADERS`."""

    status: int
    body: str | None
    headers: dict[str, str] = field(default_factory=dict)


def utcnow() -> datetime:
    """The store's clock (patched by the expiry tests)."""
    return datetime.now(UTC)


def _stale_condition(now: datetime):
    """Rows a claim may delete: past the window, or pending past the lease.
    A committed row inside the window never matches."""
    return or_(
        IdempotencyKey.created_at <= now - KEY_TTL,
        and_(
            IdempotencyKey.state == STATE_PENDING,
            IdempotencyKey.created_at <= now - PENDING_LEASE,
        ),
    )


def _is_stale(state: str, created_at: datetime, now: datetime) -> bool:
    age = now - created_at
    return age >= KEY_TTL or (state == STATE_PENDING and age >= PENDING_LEASE)


async def claim(
    db: AsyncSession, user_id: uuid.UUID, key: str, request_hash: str
) -> int | StoredResponse:
    """Claim ``key`` for this request: the new pending row's id, or the
    response to replay. Raises :class:`KeyReusedError`,
    :class:`KeyInProgressError` or :class:`KeyOutcomeUnknownError`. Commits
    (the claim, or the read). Keys are per user: another user's row with the
    same key is never read."""
    now = utcnow()
    for _ in range(_CLAIM_ATTEMPTS):
        record_id = (
            await db.execute(
                insert(IdempotencyKey)
                .values(
                    user_id=user_id,
                    key=key,
                    request_hash=request_hash,
                    state=STATE_PENDING,
                    created_at=now,
                )
                .on_conflict_do_nothing(constraint="uq_idempotency_keys_user_key")
                .returning(IdempotencyKey.id)
            )
        ).scalar_one_or_none()
        if record_id is not None:
            await db.commit()
            return record_id
        row = (
            await db.execute(
                select(
                    IdempotencyKey.id,
                    IdempotencyKey.state,
                    IdempotencyKey.request_hash,
                    IdempotencyKey.response_status,
                    IdempotencyKey.response_body,
                    IdempotencyKey.response_headers,
                    IdempotencyKey.created_at,
                ).where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == key)
            )
        ).one_or_none()
        if row is None:
            continue  # released meanwhile: insert again
        if _is_stale(row.state, row.created_at, now):
            # Re-checked in the DELETE: a row its request committed (or a
            # retry claimed afresh) meanwhile is not stale any more, and is
            # read again on the next pass.
            await db.execute(
                delete(IdempotencyKey).where(
                    IdempotencyKey.id == row.id, _stale_condition(now)
                )
            )
            continue
        # Nothing written: ends the read without a rollback, which would
        # expire the caller's already-loaded user.
        await db.commit()
        if row.request_hash != request_hash:
            raise KeyReusedError
        if row.state == STATE_PENDING:
            raise KeyInProgressError
        if row.state == STATE_COMMITTED:
            raise KeyOutcomeUnknownError
        return StoredResponse(
            status=row.response_status,
            body=row.response_body,
            headers=dict(row.response_headers or {}),
        )
    await db.commit()
    raise KeyInProgressError


def _mark_committed(session: Session, record_id: int) -> None:
    """``pending`` -> ``committed`` in the transaction being committed; see
    step 2 of the module docstring. Repeated commits keep it committed."""
    marked = session.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.id == record_id,
            IdempotencyKey.state.in_((STATE_PENDING, STATE_COMMITTED)),
        )
        .values(state=STATE_COMMITTED)
        .execution_options(synchronize_session=False)
    )
    if marked.rowcount != 1:
        logger.warning("idempotency: claim taken over, write refused")
        raise KeyLostError


def arm_commit_marker(db: AsyncSession, record_id: int) -> Callable[[], None]:
    """Mark the claimed row committed in every commit of ``db`` from now on
    (``before_commit``: inside the same transaction, so the mark and the
    write are durable together or not at all). Returns the function that
    stops it; call it before settling the key."""
    sync_session = db.sync_session

    def listener(session: Session) -> None:
        _mark_committed(session, record_id)

    event.listen(sync_session, "before_commit", listener)

    def disarm() -> None:
        if event.contains(sync_session, "before_commit", listener):
            event.remove(sync_session, "before_commit", listener)

    return disarm


async def complete(db: AsyncSession, record_id: int, response: StoredResponse) -> None:
    """Store ``response`` on the claimed row (pending or committed) and commit."""
    await db.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.id == record_id,
            IdempotencyKey.state.in_((STATE_PENDING, STATE_COMMITTED)),
        )
        .values(
            state=STATE_COMPLETE,
            response_status=response.status,
            response_body=response.body,
            response_headers=response.headers or None,
        )
    )
    await db.commit()


async def release(db: AsyncSession, record_id: int) -> None:
    """Delete the claimed row and commit, so a retry with the same key runs
    the request again; only while it is ``pending``. A ``committed`` row
    stays: its write happened, and a retry must not apply it twice."""
    await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.id == record_id, IdempotencyKey.state == STATE_PENDING
        )
    )
    await db.commit()


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete rows older than :data:`KEY_TTL`; return how many. At most
    :data:`PURGE_MAX_BATCHES` batches of :data:`PURGE_BATCH_SIZE`, one
    commit each. The ``DELETE`` re-checks the cutoff, so two instances
    running it at once only repeat each other's (empty) deletes."""
    cutoff = (now or utcnow()) - KEY_TTL
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        ids = list(
            (
                await db.execute(
                    select(IdempotencyKey.id)
                    .where(IdempotencyKey.created_at < cutoff)
                    .order_by(IdempotencyKey.created_at)
                    .limit(PURGE_BATCH_SIZE)
                )
            ).scalars()
        )
        if ids:
            result = await db.execute(
                delete(IdempotencyKey)
                .where(IdempotencyKey.id.in_(ids), IdempotencyKey.created_at < cutoff)
                .execution_options(synchronize_session=False)
            )
            total += result.rowcount or 0
        await db.commit()
        if len(ids) < PURGE_BATCH_SIZE:
            break
    return total
