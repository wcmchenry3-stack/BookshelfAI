"""The account export purge and storage sweep (#694), run by the daily
cleanup (``app/services/token_cleanup.py``).

:func:`purge_expired` deletes the file, then the row, of every export that is
over: a ``ready`` export whose link expired, or any other one older than
:data:`STALE_AFTER` (7 days). Never a row whose link still works
(``expires_at`` in the future: an email may carry it), nor one a worker
holds under a live lease (claimed less than ``LEASE`` ago: it may be about
to sign a fresh link). Each batch locks its rows (``FOR UPDATE SKIP
LOCKED``) and keeps them locked across the storage delete and the row
delete, so a worker cannot claim one in between (its claim skips locked
rows) and a row a worker holds is never picked. At most
:data:`PURGE_MAX_BATCHES` batches of :data:`PURGE_BATCH_SIZE`, one storage
call and one commit each; a storage failure rolls the batch back and the
rows wait for the next run. Without storage only rows with no file go.

:func:`sweep_orphans` then deletes any file in ``exports/`` older than
:data:`SWEEP_AGE` (8 days), whatever the table says: an account deleted
before its export expired (the row cascades away, the file does not).

Logs counts only (the caller logs them).
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account_export import OPEN_STATUSES, STATUS_READY, AccountExport
from app.services import account_export_jobs as jobs
from app.services.object_storage import ObjectStorage

# Failed or never-finished rows are kept this long (support can see them).
STALE_AFTER = timedelta(days=7)
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
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            await db.rollback()
            break
        paths = [path for _, path in rows if path]
        try:
            if paths:
                await storage.delete(paths)
        except Exception:
            await db.rollback()  # releases the locks; the rows stay
            raise
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


async def sweep_orphans(storage: ObjectStorage, now: datetime | None = None) -> int:
    """Delete files in ``exports/`` older than :data:`SWEEP_AGE`; return how
    many. Oldest first, at most :data:`SWEEP_MAX_PAGES` pages of
    :data:`PURGE_BATCH_SIZE`."""
    cutoff = (now or jobs.utcnow()) - SWEEP_AGE
    total = 0
    for _ in range(SWEEP_MAX_PAGES):
        page = await storage.list(jobs.OBJECT_PREFIX, limit=PURGE_BATCH_SIZE)
        old = [
            f"{jobs.OBJECT_PREFIX}/{obj.name}"
            for obj in page
            if obj.created_at is not None and obj.created_at < cutoff
        ]
        if old:
            await storage.delete(old)
            total += len(old)
        if len(old) < PURGE_BATCH_SIZE:
            break
    return total
