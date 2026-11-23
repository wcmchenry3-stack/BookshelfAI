"""The account export purge and storage sweep (#694), run by the daily
cleanup (``app/services/token_cleanup.py``).

:func:`purge_expired` deletes the file, then the row, of every export that is
over: a ``ready`` export whose link expired, or any other one older than
:data:`STALE_AFTER` (7 days). Never a row whose link still works
(``expires_at`` in the future: an email may carry it), nor one a worker
holds under a live lease (claimed less than ``LEASE`` ago: it may be about
to sign a fresh link). The worker never claims a row older than
:data:`STALE_AFTER` (``claim_next`` marks it ``failed``), so the purge's rows
and the worker's are disjoint and the purge locks nothing: each batch is an
unlocked select (its transaction ended), the storage delete, then a DELETE
that re-checks the condition; no database transaction is open across the
storage call. At most :data:`PURGE_MAX_BATCHES` batches of
:data:`PURGE_BATCH_SIZE`. A storage failure is logged and ends the purge (the
rows stay for the next run); the caller still runs the sweep. Without storage
only rows with no file go.

:func:`sweep_orphans` then deletes any file in ``exports/`` older than
:data:`SWEEP_AGE` (8 days), whatever the table says: an account deleted
before its export expired (the row cascades away, the file does not), or a
file left by a try that failed before its row recorded it. A file a row
still points at is never swept, whatever its age.

Logs counts only (the caller logs them).
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_export import OPEN_STATUSES, STATUS_READY, AccountExport
from app.services import account_export_jobs as jobs
from app.services.object_storage import ObjectStorage, StorageError

logger = logging.getLogger(__name__)

# Failed or never-finished rows are kept this long (support can see them);
# the worker stops claiming a row at this age (account_export_jobs).
STALE_AFTER = jobs.STALE_AFTER
# Files older than this are deleted whether or not a row knows them.
SWEEP_AGE = timedelta(days=8)
PURGE_BATCH_SIZE = 100
PURGE_MAX_BATCHES = 10
SWEEP_MAX_PAGES = 10


def expired_condition(now: datetime):
    """Exports that are over and whose file nobody can still use."""
    link_dead = or_(AccountExport.expires_at.is_(None), AccountExport.expires_at < now)
    leased = and_(
        AccountExport.status.in_(OPEN_STATUSES),
        AccountExport.claimed_at.is_not(None),
        AccountExport.claimed_at >= now - jobs.LEASE,
    )
    over = or_(
        and_(
            AccountExport.status == STATUS_READY, AccountExport.expires_at.is_not(None)
        ),
        AccountExport.created_at < now - STALE_AFTER,
    )
    return and_(over, link_dead, not_(leased))


async def purge_expired(
    db: AsyncSession,
    storage: ObjectStorage | None,
    now: datetime | None = None,
) -> int:
    """Delete expired exports (file first, then row); return how many rows
    went. See the module docstring."""
    now = now or jobs.utcnow()
    condition = expired_condition(now)
    if storage is None:
        condition = and_(condition, AccountExport.object_path.is_(None))
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        rows = (
            await db.execute(
                select(AccountExport.id, AccountExport.object_path)
                .where(condition)
                .order_by(AccountExport.created_at)
                .limit(PURGE_BATCH_SIZE)
            )
        ).all()
        # End the read's transaction: none may be open across the storage call.
        await db.rollback()
        if not rows:
            break
        paths = [path for _, path in rows if path]
        try:
            if paths:
                await storage.delete(paths)
        except StorageError as exc:
            # ERROR, not WARNING: Sentry's logging integration only turns
            # ERROR and above into events, and a persistent failure here keeps
            # expired personal data past its 7-day retention (#1058 review).
            logger.error(
                "account_export: purge could not delete %d file(s) (%s); "
                "the rows stay for the next run",
                len(paths),
                exc,
            )
            break
        result = await db.execute(
            delete(AccountExport)
            .where(AccountExport.id.in_([row_id for row_id, _ in rows]), condition)
            .execution_options(synchronize_session=False)
        )
        total += result.rowcount or 0
        await db.commit()
        if len(rows) < PURGE_BATCH_SIZE:
            break
    return total


async def sweep_orphans(
    storage: ObjectStorage, db: AsyncSession, now: datetime | None = None
) -> int:
    """Delete files in ``exports/`` older than :data:`SWEEP_AGE` that no
    ``account_exports`` row points at (one lookup per page); return how
    many. Oldest first, at most :data:`SWEEP_MAX_PAGES` pages of
    :data:`PURGE_BATCH_SIZE`; a file a row still uses is kept and skipped
    (the next page starts after it)."""
    cutoff = (now or jobs.utcnow()) - SWEEP_AGE
    total = 0
    offset = 0
    for _ in range(SWEEP_MAX_PAGES):
        page = await storage.list(
            jobs.OBJECT_PREFIX, limit=PURGE_BATCH_SIZE, offset=offset
        )
        old = [
            f"{jobs.OBJECT_PREFIX}/{obj.name}"
            for obj in page
            if obj.created_at is not None and obj.created_at < cutoff
        ]
        known: set[str] = set()
        if old:
            known = set(
                (
                    await db.execute(
                        select(AccountExport.object_path).where(
                            AccountExport.object_path.in_(old)
                        )
                    )
                ).scalars()
            )
            await db.rollback()
        orphans = [path for path in old if path not in known]
        if orphans:
            await storage.delete(orphans)
            total += len(orphans)
        # Oldest first: a page with a younger file, or a short one, is the end.
        if len(page) < PURGE_BATCH_SIZE or len(old) < len(page):
            break
        offset += len(page) - len(orphans)
    return total
