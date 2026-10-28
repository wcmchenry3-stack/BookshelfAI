"""Refresh-token families and reuse detection (#690).

Every sign-in starts a *family*: the first refresh row's id is the family id,
and each rotation's successor row inherits it. A rotation marks the old row
``used_at`` (and ``revoked_at``) and points ``replaced_by_id`` at the
successor. Raw tokens are never stored; rows are keyed by the ``jti`` claim
as before.

When ``/auth/refresh`` is handed a token whose row was already rotated, it is
one of four cases (:func:`classify_replay`):

- ``RACE``: the row was still live when this request first read it, so a
  concurrent request with the same token (its *twin*) rotated it while this
  one waited for the user-row lock, and the twin's successor is still live
  and unused. Handled like ``RETRY``: this request gets a fresh pair in the
  same family and the twin's successor is *displaced*. The two concurrent
  requests may be one client's double refresh, or a thief with a copy of a
  live token racing the real client; either way only one of the two pairs
  stays usable, and the other one's holder trips ``REUSE`` on its next
  refresh, so a thief who wins the race no longer takes over undetected.
  No alert. If the twin's successor has itself been rotated or displaced
  (a third request with the same token got there first), that is
  ``REUSE``: one client single-flights its refreshes, so three requests
  with one token means a copy. If the twin's successor was revoked without
  rotation (a logout) or deleted, the plain 401 from before #690
  (``ENDED``).
- ``RETRY``: rotated at most ``refresh_reuse_grace_seconds`` ago and the
  successor is still live and unused, so the client most likely never got the
  response (lost on the network, app suspended). The server cannot return the
  same successor (it stores no raw token), so it mints a fresh pair in the
  same family and *displaces* the successor.
- ``ENDED``: within the window, but the successor was revoked without being
  rotated (a logout) or has been deleted. The session is over; plain 401.
- ``REUSE``: outside the window; or the successor was already rotated (the
  chain moved on without this token); or the token itself was displaced; or
  the successor was displaced (this token already had its one retry). Someone
  else holds a copy. The caller revokes every session
  (``revoke_all_sessions``: the family's rows and the user's access tokens,
  via a token_version bump), answers 401 ``refresh_reused`` and calls
  :func:`alert_reuse`.

Displacing (``RACE``/``RETRY``) marks the successor rotated (``revoked_at``,
``used_at``) and ``displaced_at``, and points its ``replaced_by_id`` at the
new row. The rotated token's own ``replaced_by_id`` keeps pointing at the
displaced row, so:

- presenting the displaced token is always ``REUSE``, with no grace window
  (whoever holds it is not the holder of the replacement);
- presenting the rotated token again finds a displaced successor: ``REUSE``.
  Each rotated token gets at most one retry, so two holders cannot take turns
  displacing each other inside the window.
"""

import enum
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.alerts import capture_service_warning
from app.models.refresh_token import RefreshToken

logger = logging.getLogger(__name__)

# 401 detail for a rotated refresh token presented again outside the grace
# window. The client signs out, as on any refresh 401.
REFRESH_REUSED = "refresh_reused"

# 401 detail for an unknown or revoked refresh token (unchanged from before
# #690; also the answer to ENDED, including a concurrent refresh whose twin's
# successor was signed out or deleted).
REFRESH_REVOKED_OR_NOT_FOUND = "Refresh token revoked or not found"

REUSE_ALERT_MESSAGE = "Refresh token reuse detected; all sessions revoked"
REUSE_ALERT_FINGERPRINT = ("auth-refresh-token-reuse",)


class Replay(enum.Enum):
    RACE = "race"
    RETRY = "retry"
    ENDED = "ended"
    REUSE = "reuse"


def family_of(row: RefreshToken) -> uuid.UUID:
    """The row's family id. Rows written during the deploy by code from
    before migration 0022 have none; each is its own family, as the
    migration's backfill does."""
    return row.family_id or row.id


def new_family_row(user_id: uuid.UUID, jti: str, expires_at: datetime) -> RefreshToken:
    """The first row of a new family (a sign-in): its id is the family id."""
    row_id = uuid.uuid4()
    return RefreshToken(
        id=row_id,
        jti=jti,
        user_id=user_id,
        expires_at=expires_at,
        family_id=row_id,
    )


def successor_row(
    previous: RefreshToken, jti: str, expires_at: datetime
) -> RefreshToken:
    """A row that continues ``previous``'s family."""
    return RefreshToken(
        id=uuid.uuid4(),
        jti=jti,
        user_id=previous.user_id,
        expires_at=expires_at,
        family_id=family_of(previous),
    )


def is_live(row: RefreshToken) -> bool:
    return row.revoked_at is None and row.used_at is None


def is_displaced(row: RefreshToken) -> bool:
    """Replaced by a retry (or a won race) of the token before it: never
    valid again, and presenting it is reuse."""
    return row.displaced_at is not None


def is_rotated(row: RefreshToken) -> bool:
    """Rotated by /auth/refresh: rotation sets both ``revoked_at`` and
    ``used_at``. A row revoked any other way (logout, logout-all, a stale
    ``tv``) has no ``used_at``; rows rotated before migration 0022 have none
    either and count as plainly revoked."""
    return row.revoked_at is not None and row.used_at is not None


def classify_replay(
    *,
    seen_rotated: bool,
    displaced: bool,
    used_at: datetime,
    successor: RefreshToken | None,
    now: datetime,
    grace_seconds: int,
) -> Replay:
    """What a rotated token presented again is (see the module docstring).

    ``seen_rotated`` is whether the row was already rotated when this request
    first read it, before waiting for the user-row lock; ``displaced`` is
    whether the row itself was displaced by a retry; ``successor`` is the row
    its ``replaced_by_id`` points at (``None`` if deleted).
    """
    if displaced:
        return Replay.REUSE
    if not seen_rotated:
        if successor is not None and is_live(successor):
            return Replay.RACE
        if successor is not None and is_rotated(successor):
            return Replay.REUSE  # a third request got there first
        return Replay.ENDED
    if now - used_at > timedelta(seconds=grace_seconds):
        return Replay.REUSE
    if successor is None:
        return Replay.ENDED
    if is_displaced(successor):
        return Replay.REUSE  # this token already had its one retry
    if is_live(successor):
        return Replay.RETRY
    if not is_rotated(successor):
        return Replay.ENDED  # revoked without rotation: signed out
    return Replay.REUSE


def alert_reuse(user_id: uuid.UUID, family_id: uuid.UUID) -> None:
    """Log and send one Sentry warning for a detected reuse.

    Carries the user id and family id only: never a token, jti or email.
    The event is captured without the request (``capture_service_warning``
    drops the request block, user, breadcrumbs and transaction), so the
    refresh token in the request body cannot reach it. Never raises.
    """
    logger.warning(
        "Refresh token reuse detected; all sessions revoked "
        "(user_id=%s family_id=%s)",
        user_id,
        family_id,
    )
    try:
        capture_service_warning(
            REUSE_ALERT_MESSAGE,
            fingerprint=REUSE_ALERT_FINGERPRINT,
            tags={"user_id": str(user_id), "family_id": str(family_id)},
            extras={},
        )
    except Exception as exc:  # noqa: BLE001 - an alert must not fail the 401
        logger.warning("Refresh reuse alert not sent (%s)", type(exc).__name__)


# Cleanup (app/services/token_cleanup.py): bounded batches, like the other
# daily purges (docs/storage-and-retention.md).
PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20


def purgeable(now: datetime):
    """Rows the daily cleanup deletes: expired ones (the JWT can no longer
    be decoded), and ones revoked without rotation (logout, logout-all, a
    stale ``tv``), which nothing reads again. Rotated rows stay until they
    expire: reuse detection needs them for the token's whole lifetime."""
    return or_(
        RefreshToken.expires_at < now,
        and_(RefreshToken.revoked_at.is_not(None), RefreshToken.used_at.is_(None)),
    )


async def purge_refresh_tokens(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete :func:`purgeable` rows in batches of :data:`PURGE_BATCH_SIZE`,
    at most :data:`PURGE_MAX_BATCHES` per run, committing each batch; return
    how many were deleted.

    A row deleted while another row's ``replaced_by_id`` points at it sets
    that pointer to NULL (FK ``ON DELETE SET NULL``), which needs that
    predecessor's row lock. /auth/refresh locks a replayed row and then its
    successor; deleting the successor first and then waiting for the
    predecessor would deadlock with it. So the purge never waits for a lock:
    it locks its batch ``FOR UPDATE SKIP LOCKED``, then the batch's
    predecessors the same way, and leaves out (until a later run) any row
    that is locked or has a predecessor that is."""
    cutoff = now or datetime.now(UTC)
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        candidates = list(
            (
                await db.execute(
                    select(RefreshToken.id)
                    .where(purgeable(cutoff))
                    .limit(PURGE_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        deletable = await _without_locked_predecessors(db, candidates)
        deleted = 0
        if deletable:
            result = await db.execute(
                delete(RefreshToken).where(RefreshToken.id.in_(deletable))
            )
            deleted = result.rowcount or 0
        await db.commit()
        total += deleted
        if len(candidates) < PURGE_BATCH_SIZE:
            break
    return total


async def _without_locked_predecessors(
    db: AsyncSession, candidates: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Lock the rows whose ``replaced_by_id`` is one of ``candidates``
    (``SKIP LOCKED``) and return the candidates whose predecessors are all
    locked by this transaction. Rows already locked here (a candidate that
    is another's predecessor) are not skipped."""
    if not candidates:
        return []
    points_at = RefreshToken.replaced_by_id.in_(candidates)
    locked = set(
        (
            await db.execute(
                select(RefreshToken.id)
                .where(points_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    predecessors = (
        await db.execute(
            select(RefreshToken.id, RefreshToken.replaced_by_id).where(points_at)
        )
    ).all()
    blocked = {succ for pred, succ in predecessors if pred not in locked}
    return [c for c in candidates if c not in blocked]
