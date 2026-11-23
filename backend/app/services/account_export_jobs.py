"""Account export requests, the export worker and the export purge (#694).

Request (``POST /account/export``, :func:`request_export`): under the user's
row lock (the cap must not let two concurrent requests through), refuse when
the user has an export inside :data:`CAP_WINDOW` (24 h) that did not fail
(:class:`ExportLimited` with the seconds until the next one is allowed; a
``failed`` export emailed nothing, so it does not count), else insert a
``pending`` ``account_exports`` row. Nothing heavy runs in the request.

Worker (:func:`run_worker`, started from the app lifespan): wakes when a
request was queued on this instance (:func:`wake`) and every
:data:`POLL_SECONDS` anyway, so a row queued before a restart, or on another
instance, is never lost. Each pass claims rows one at a time
(:func:`claim_next`: ``FOR UPDATE SKIP LOCKED``, so two instances never take
the same row; a claim is a lease of :data:`LEASE`, so a run that died is
taken over later) and for each (:func:`process`):

1. builds the document (``app/services/account_export.py``) in a
   ``JobSessionLocal`` session (the 60 s job statement timeout), uploads it
   to the private bucket at ``exports/<export id>.json``
   (``app/services/object_storage.py``) and signs a download URL valid for
   :data:`LINK_TTL` (7 days);
2. records the path, the link and ``expires_at`` (signing time + 7 days)
   and moves the row to ``emailing``, in one commit, before any email;
3. emails the recorded link to the account's address in the request's
   language (``app/services/export_email.py``, ``app/services/email.py``)
   with ``account-export-<id>`` as Resend's idempotency key;
4. marks the row ``ready``.

Delivery is idempotent: a retry of an ``emailing`` row re-renders the same
email from the recorded link, which Resend (same key, same payload, within
24 h) does not send again; its 409 ``invalid_idempotent_request`` (the key
went out with another payload, e.g. a changed address) counts as sent. A
lost response, a failed final commit or a kill before it ends with one
email and the row ``ready`` at the next attempt.

Every write of an attempt after its claim is fenced: it applies only while
the row is still ``running`` / ``emailing`` with this attempt's
``claimed_at`` (:class:`Claim`), so an attempt whose lease was taken over
cannot reopen, fail or finish the row, nor delete its file.

A failed attempt keeps an ``error_code`` and waits out the lease before the
next try: a row that had not reached ``emailing`` goes back to ``pending``,
an ``emailing`` row stays ``emailing``. After :data:`MAX_ATTEMPTS` it is
``failed``; its file is deleted only if it never reached ``emailing``
(otherwise a delivered link keeps working until ``expires_at``).

Purge (:func:`purge_expired`, from the daily cleanup in
``app/services/token_cleanup.py``): for rows whose link expired, failed or
never-finished rows older than 7 days (and past ``expires_at`` when they
have one), deletes the objects first and then
the rows, in bounded batches. :func:`sweep_orphans` then deletes any object
older than :data:`SWEEP_AGE` (8 days) left in ``exports/``, which covers an
account deleted before its export expired (the row cascades away, the
object does not). The signed link expires on its own after 7 days, so the
link stops working on time even when the purge runs late.

Kill switch: without ``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY`` /
``RESEND_API_KEY`` (:func:`configured`) the endpoint answers 503
``export_not_configured`` and the worker does nothing. Logs carry the user
id and export id only: never the email address, the link or a value from
the document.
"""

import asyncio
import json
import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import JobSessionLocal
from app.models.account_export import (
    OPEN_STATUSES,
    STATUS_EMAILING,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_RUNNING,
    AccountExport,
)
from app.models.user import User
from app.services import account_export, email, export_email, object_storage
from app.services.email import EmailError
from app.services.object_storage import ObjectStorage, StorageError
from app.services.unverified_books import lock_user

logger = logging.getLogger(__name__)

# One export a day per user.
CAP_WINDOW = timedelta(hours=24)
# The signed link, and the file, live 7 days.
LINK_TTL = timedelta(days=7)
LINK_TTL_SECONDS = 604_800
# A claimed row is presumed dead (and a failed one may retry) after this.
LEASE = timedelta(minutes=10)
MAX_ATTEMPTS = 3
# Exports handled per worker pass (each one is a few seconds).
MAX_PER_PASS = 20
POLL_SECONDS = 300
INITIAL_DELAY_SECONDS = 30
# Folder of the export objects inside the bucket.
OBJECT_PREFIX = "exports"
# Failed or never-finished rows are kept this long (support can see them).
STALE_AFTER = timedelta(days=7)
# Objects older than this are deleted whether or not a row knows them.
SWEEP_AGE = timedelta(days=8)
PURGE_BATCH_SIZE = 100
PURGE_MAX_BATCHES = 10
SWEEP_MAX_PAGES = 10
CONTENT_TYPE = "application/json"

Sessions = async_sessionmaker[AsyncSession]


class ExportLimited(Exception):
    """The user asked within the cap window; ``retry_after`` seconds left."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("account export limited")
        self.retry_after = retry_after


def utcnow() -> datetime:
    """The jobs' clock (patched by the tests)."""
    return datetime.now(UTC)


def configured() -> bool:
    """Whether storage and email are both set up (the kill switch)."""
    return object_storage.from_settings() is not None and email.configured()


def object_path(export_id: uuid.UUID) -> str:
    return f"{OBJECT_PREFIX}/{export_id}.json"


async def request_export(
    db: AsyncSession, user: User, locale: str, now: datetime | None = None
) -> AccountExport:
    """Queue an export for ``user`` (flushed, not committed), or raise
    :class:`ExportLimited`."""
    now = now or utcnow()
    await lock_user(db, user.id)
    latest = await db.scalar(
        select(AccountExport.created_at)
        .where(
            AccountExport.user_id == user.id,
            AccountExport.created_at > now - CAP_WINDOW,
            # A failed export emailed nothing: the user may ask again at once.
            AccountExport.status != STATUS_FAILED,
        )
        .order_by(AccountExport.created_at.desc())
        .limit(1)
    )
    if latest is not None:
        wait = (latest + CAP_WINDOW - now).total_seconds()
        raise ExportLimited(max(1, math.ceil(wait)))
    export = AccountExport(
        id=uuid.uuid4(),
        user_id=user.id,
        status=STATUS_PENDING,
        locale=locale,
        attempts=0,
        created_at=now,
    )
    db.add(export)
    await db.flush()
    return export


# ── Worker ───────────────────────────────────────────────────────────────────

_wake_event: asyncio.Event | None = None
_wake_loop: asyncio.AbstractEventLoop | None = None


def wake() -> None:
    """Ask this instance's worker to run now (a request was just queued).
    Does nothing when no worker runs here (tests, scripts)."""
    if _wake_event is None or _wake_loop is None or _wake_loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _wake_loop:
        _wake_event.set()
    else:
        _wake_loop.call_soon_threadsafe(_wake_event.set)


def _open_condition(now: datetime):
    """Rows the worker may take: not finished, and not held by a live lease
    (a failed try waits out the lease before the next)."""
    return and_(
        AccountExport.status.in_(OPEN_STATUSES),
        or_(
            AccountExport.claimed_at.is_(None),
            AccountExport.claimed_at < now - LEASE,
        ),
    )


@dataclass(frozen=True)
class Claim:
    """One attempt's hold on a row: the row, and the ``claimed_at`` this
    attempt wrote. Every later write of the attempt is fenced on both, so an
    attempt whose lease was taken over can no longer change the row."""

    id: uuid.UUID
    claimed_at: datetime


def _owned(claim: Claim):
    return and_(
        AccountExport.id == claim.id,
        AccountExport.status.in_((STATUS_RUNNING, STATUS_EMAILING)),
        AccountExport.claimed_at == claim.claimed_at,
    )


async def claim_next(db: AsyncSession, now: datetime | None = None) -> Claim | None:
    """Claim the oldest open row (commits); ``None`` when there is none. A
    row out of attempts is marked ``failed`` instead and the next is tried.
    An ``emailing`` row stays ``emailing`` (its email may have gone out)."""
    now = now or utcnow()
    while True:
        export = await db.scalar(
            select(AccountExport)
            .where(_open_condition(now))
            .order_by(AccountExport.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if export is None:
            await db.commit()
            return None
        if export.attempts >= MAX_ATTEMPTS:
            # An emailing row keeps its file: the purge removes it after
            # expires_at, so a link that may have been delivered still works.
            export.status = STATUS_FAILED
            export.claimed_at = None
            export.error_code = export.error_code or "attempts_exhausted"
            await db.commit()
            logger.warning(
                "account_export: gave up (export_id=%s user_id=%s)",
                export.id,
                export.user_id,
            )
            continue
        if export.status != STATUS_EMAILING:
            export.status = STATUS_RUNNING
        export.claimed_at = now
        export.attempts += 1
        await db.commit()
        return Claim(export.id, now)


async def _fenced_update(sessions: Sessions, claim: Claim, **values) -> bool:
    """Apply ``values`` only while ``claim`` still holds the row; True when it
    did (one row changed)."""
    async with sessions() as db:
        result = await db.execute(
            update(AccountExport)
            .where(_owned(claim))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    return (result.rowcount or 0) == 1


async def _row_exists(sessions: Sessions, export_id: uuid.UUID) -> bool:
    async with sessions() as db:
        found = await db.scalar(
            select(AccountExport.id).where(AccountExport.id == export_id)
        )
        await db.rollback()
    return found is not None


def _error_code(exc: Exception) -> str:
    if isinstance(exc, StorageError):
        return f"storage_{exc.operation}_{exc.status or 'unreachable'}"
    if isinstance(exc, EmailError):
        return f"email_{exc.status or 'unreachable'}"
    return "unexpected_error"


async def _prepare(
    sessions: Sessions, claim: Claim, user_id: uuid.UUID, storage: ObjectStorage
) -> tuple[str, datetime] | None:
    """Build, upload and sign the file, then record the link and move the row
    to ``emailing`` (fenced). Returns the link and its expiry, or ``None``
    when this attempt no longer holds the row."""
    async with sessions() as db:
        document = await account_export.build(db, user_id)
        await db.rollback()
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    path = object_path(claim.id)
    await storage.upload(path, data, CONTENT_TYPE)
    # Recorded at once, so a later failure (signing) can still delete it.
    if not await _fenced_update(
        sessions, claim, object_path=path, size_bytes=len(data)
    ):
        return await _lost(sessions, claim, path, storage)
    signed_at = utcnow()
    url = await storage.signed_url(
        path,
        LINK_TTL_SECONDS,
        download_name=f"bookshelf-export-{signed_at:%Y-%m-%d}.json",
    )
    expires_at = signed_at + LINK_TTL
    # From this commit on an email may go out, so the file is kept until
    # expires_at whatever happens next.
    if await _fenced_update(
        sessions,
        claim,
        status=STATUS_EMAILING,
        delivery_url=url,
        expires_at=expires_at,
    ):
        return url, expires_at
    return await _lost(sessions, claim, path, storage)


async def _lost(
    sessions: Sessions, claim: Claim, path: str, storage: ObjectStorage
) -> None:
    """This attempt no longer holds the row. Another attempt writes the same
    path, so the file is left alone unless the row is gone with its account."""
    if not await _row_exists(sessions, claim.id):
        await storage.delete([path])


async def _send(
    claim: Claim,
    message: email.Message,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Send the email once per export: the same key and the same payload on
    every retry, so Resend sends it once. A 409 for a key it accepted with a
    different payload (the address changed meanwhile) means the first one
    went out."""
    try:
        await email.send(
            message, idempotency_key=f"account-export-{claim.id}", transport=transport
        )
    except EmailError as exc:
        if exc.code != email.KEY_ALREADY_USED:
            raise
        logger.info(
            "account_export: email already sent under this key (export_id=%s)",
            claim.id,
        )


async def _mark_ready(sessions: Sessions, claim: Claim) -> bool:
    return await _fenced_update(
        sessions,
        claim,
        status=STATUS_READY,
        completed_at=utcnow(),
        claimed_at=None,
        error_code=None,
    )


async def _attempt(
    sessions: Sessions,
    claim: Claim,
    storage: ObjectStorage,
    email_transport: httpx.AsyncBaseTransport | None,
) -> bool:
    """One try. True when this attempt delivered the export; False when there
    is nothing left for it to do (the row went away, or another attempt holds
    it). Raises on a storage, email or build failure."""
    async with sessions() as db:
        export = await db.get(AccountExport, claim.id)
        user = await db.get(User, export.user_id) if export else None
        if export is None or user is None:
            return False  # the account (and its row) went away
        to, locale, user_id = user.email, export.locale, user.id
        url, expires_at = export.delivery_url, export.expires_at
        await db.rollback()
    if url is None or expires_at is None:
        prepared = await _prepare(sessions, claim, user_id, storage)
        if prepared is None:
            return False
        url, expires_at = prepared
    await _send(
        claim, export_email.render(locale, to, url, expires_at), email_transport
    )
    return await _mark_ready(sessions, claim)


async def _record_failure(
    sessions: Sessions, claim: Claim, code: str, storage: ObjectStorage
) -> None:
    """Record a failed try, fenced on ``claim``: a stale attempt changes
    nothing. Before the link was recorded the row goes back to ``pending``;
    an ``emailing`` row stays ``emailing``; either waits out the lease. The
    last attempt marks it ``failed`` and deletes the file only if no email
    can have gone out."""
    async with sessions() as db:
        export = await db.scalar(
            select(AccountExport).where(_owned(claim)).with_for_update()
        )
        if export is None:
            await db.rollback()
            return
        export.error_code = code
        final = export.attempts >= MAX_ATTEMPTS
        emailing = export.status == STATUS_EMAILING
        user_id, path = export.user_id, export.object_path
        if final:
            export.status = STATUS_FAILED
            export.claimed_at = None
        elif not emailing:
            export.status = STATUS_PENDING
        await db.commit()
    if not final:
        return
    logger.error(
        "account_export: failed for good (export_id=%s user_id=%s code=%s)",
        claim.id,
        user_id,
        code,
    )
    if path and not emailing:
        try:
            await storage.delete([path])
        except StorageError:
            logger.warning(
                "account_export: could not delete a failed export's object "
                "(export_id=%s); the purge retries",
                claim.id,
            )


async def process(
    claim: Claim,
    storage: ObjectStorage,
    *,
    sessions: Sessions = JobSessionLocal,
    email_transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Deliver one claimed export; True when this attempt delivered it. Never
    raises except ``asyncio.CancelledError`` (which leaves the row to be
    taken over after the lease)."""
    try:
        delivered = await _attempt(sessions, claim, storage, email_transport)
    except (StorageError, EmailError) as exc:
        code = _error_code(exc)
        logger.warning(
            "account_export: attempt failed (export_id=%s code=%s)", claim.id, code
        )
    except Exception:
        code = "unexpected_error"
        logger.exception("account_export: attempt failed (export_id=%s)", claim.id)
    else:
        if delivered:
            logger.info("account_export: delivered (export_id=%s)", claim.id)
        return delivered
    try:
        await _record_failure(sessions, claim, code, storage)
    except Exception:
        logger.exception(
            "account_export: could not record a failure (export_id=%s)", claim.id
        )
    return False


async def process_pending(
    *,
    sessions: Sessions = JobSessionLocal,
    storage: ObjectStorage | None = None,
    email_transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """One worker pass: deliver up to :data:`MAX_PER_PASS` open exports;
    return how many were tried. Does nothing while not :func:`configured`."""
    if not configured():
        return 0
    storage = storage or object_storage.from_settings()
    tried = 0
    for _ in range(MAX_PER_PASS):
        async with sessions() as db:
            claim = await claim_next(db)
        if claim is None:
            break
        await process(
            claim, storage, sessions=sessions, email_transport=email_transport
        )
        tried += 1
    return tried


async def run_worker() -> None:
    """Long-running coroutine; meant to be wrapped in asyncio.create_task()."""
    global _wake_event, _wake_loop
    _wake_loop = asyncio.get_running_loop()
    _wake_event = asyncio.Event()
    delay = INITIAL_DELAY_SECONDS
    while True:
        try:
            await asyncio.wait_for(_wake_event.wait(), delay)
        except TimeoutError:
            pass
        _wake_event.clear()
        delay = POLL_SECONDS
        try:
            await process_pending()
        except Exception:
            # The next pass retries; a database outage must not end the loop.
            logger.exception("account_export: worker pass failed")


# ── Purge ────────────────────────────────────────────────────────────────────


def _expired_condition(now: datetime):
    return or_(
        and_(AccountExport.status == STATUS_READY, AccountExport.expires_at < now),
        and_(
            AccountExport.status != STATUS_READY,
            AccountExport.created_at < now - STALE_AFTER,
            # An email may have gone out: keep the file while its link works.
            or_(AccountExport.expires_at.is_(None), AccountExport.expires_at < now),
        ),
    )


async def purge_expired(
    db: AsyncSession,
    storage: ObjectStorage | None,
    now: datetime | None = None,
) -> int:
    """Delete expired exports (object first, then row); return how many rows
    went. At most :data:`PURGE_MAX_BATCHES` batches of
    :data:`PURGE_BATCH_SIZE`, one storage call and one commit each. Without
    storage only rows with no object go. A storage failure stops the run;
    those rows stay for the next one."""
    now = now or utcnow()
    condition = _expired_condition(now)
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
        await db.rollback()  # no transaction held over the storage call
        if not rows:
            break
        paths = [path for _, path in rows if path]
        if paths:
            await storage.delete(paths)
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
    """Delete objects in ``exports/`` older than :data:`SWEEP_AGE`, whatever
    the table says; return how many. Oldest first, at most
    :data:`SWEEP_MAX_PAGES` pages of :data:`PURGE_BATCH_SIZE`."""
    cutoff = (now or utcnow()) - SWEEP_AGE
    total = 0
    for _ in range(SWEEP_MAX_PAGES):
        page = await storage.list(OBJECT_PREFIX, limit=PURGE_BATCH_SIZE)
        old = [
            f"{OBJECT_PREFIX}/{obj.name}"
            for obj in page
            if obj.created_at is not None and obj.created_at < cutoff
        ]
        if old:
            await storage.delete(old)
            total += len(old)
        if len(old) < PURGE_BATCH_SIZE:
            break
    return total
