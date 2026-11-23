"""Account export requests and the export worker (#694).

Request (``POST /account/export``, :func:`request_export`): under the user's
row lock (the cap must not let two concurrent requests through), refuse when
the user has an open or ready export inside :data:`CAP_WINDOW` (24 h), or
:data:`FAILED_CAP` (3) failed ones (:class:`ExportLimited` with the seconds
until the next one is allowed; a failed export emailed nothing, so it gets
the looser cap, which still bounds the work a failing export can cost),
else insert a
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
   to the private bucket at a new ``exports/<export id>-<ms>.json`` (a
   re-signed export gets a new file; the old one is deleted)
   (``app/services/object_storage.py``) and signs a download URL valid for
   :data:`LINK_TTL` (7 days);
2. records the path, the link and ``expires_at`` (signing time + 7 days)
   and moves the row to ``emailing``, in one commit, before any email;
3. emails the recorded link to the account's address in the request's
   language (``app/services/export_email.py``, ``app/services/email.py``)
   with ``account-export-<id>-<link expiry>`` as Resend's idempotency key
   (:func:`idempotency_key`; set ``email_accepted_at`` when Resend accepted
   it, or may have: a timeout or a 5xx);
4. marks the row ``ready``.

Delivery is idempotent: a retry of an ``emailing`` row re-renders the same
email from the recorded link, which Resend (same key, same payload, within
24 h) does not send again; its 409 ``invalid_idempotent_request`` (the key
went out with another payload, e.g. a changed address) counts as sent, but
only when ``email_accepted_at`` says an earlier try may have been accepted.
A lost response, a failed final commit or a kill before it ends with one
email and the row ``ready`` at the next attempt. A recorded link with less
than :data:`MIN_LINK_LEFT` (1 h) left is never emailed: a new one is signed.

Every write of an attempt after its claim is fenced: it applies only while
the row is still ``running`` / ``emailing`` with this attempt's
``claimed_at`` (:class:`Claim`), so an attempt whose lease was taken over
cannot reopen, fail or finish the row, nor delete its file.

A failed attempt keeps an ``error_code`` and waits out the lease before the
next try: a row that had not reached ``emailing`` goes back to ``pending``,
an ``emailing`` row stays ``emailing``. After :data:`MAX_ATTEMPTS` it is
``failed``; its file is deleted only if it never reached ``emailing``
(otherwise a delivered link keeps working until ``expires_at``). A row that
runs out of attempts while ``emailing`` ends ``failed`` even if its last
send was accepted; the user can ask again (failed exports have a looser
cap), so that is accepted.

The purge and the storage sweep are in ``app/services/account_export_purge.py``.

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
from sqlalchemy import and_, or_, select, update
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
# A recorded link with less than this left is not emailed; a new one is
# signed instead.
MIN_LINK_LEFT = timedelta(hours=1)
# Failed exports a user may have inside CAP_WINDOW (an open or ready one
# allows no other).
FAILED_CAP = 3
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


def object_path(export_id: uuid.UUID, uploaded_at: datetime) -> str:
    """A new path for every upload (Supabase keeps an object's first
    ``created_at`` on an overwrite, so a re-signed export must not reuse its
    old path, or the age-based sweep would delete its live file)."""
    return f"{OBJECT_PREFIX}/{export_id}-{int(uploaded_at.timestamp() * 1000)}.json"


def _next_allowed(recent) -> datetime | None:
    """When the next request is allowed, given ``(status, created_at)`` of
    the user's exports inside the window; ``None`` when one is allowed now.
    An open or ready export allows no other for 24 hours; a failed one
    emailed nothing, so it only counts toward :data:`FAILED_CAP`."""
    live = [created for status, created in recent if status != STATUS_FAILED]
    failed = sorted(created for status, created in recent if status == STATUS_FAILED)
    times = []
    if live:
        times.append(max(live) + CAP_WINDOW)
    if len(failed) >= FAILED_CAP:
        times.append(failed[-FAILED_CAP] + CAP_WINDOW)
    return max(times) if times else None


async def request_export(
    db: AsyncSession, user: User, locale: str, now: datetime | None = None
) -> AccountExport:
    """Queue an export for ``user`` (flushed, not committed), or raise
    :class:`ExportLimited`."""
    now = now or utcnow()
    await lock_user(db, user.id)
    recent = (
        await db.execute(
            select(AccountExport.status, AccountExport.created_at).where(
                AccountExport.user_id == user.id,
                AccountExport.created_at > now - CAP_WINDOW,
            )
        )
    ).all()
    allowed_at = _next_allowed(recent)
    if allowed_at is not None and allowed_at > now:
        wait = (allowed_at - now).total_seconds()
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


async def _row_path(sessions: Sessions, export_id: uuid.UUID) -> str | None:
    """The row's ``object_path`` (``None`` also when the row is gone)."""
    async with sessions() as db:
        path = await db.scalar(
            select(AccountExport.object_path).where(AccountExport.id == export_id)
        )
        await db.rollback()
    return path


def _error_code(exc: Exception) -> str:
    if isinstance(exc, StorageError):
        return f"storage_{exc.operation}_{exc.status or 'unreachable'}"
    if isinstance(exc, EmailError):
        return f"email_{exc.status or 'unreachable'}"
    return "unexpected_error"


async def _prepare(
    sessions: Sessions,
    claim: Claim,
    user_id: uuid.UUID,
    old_path: str | None,
    storage: ObjectStorage,
) -> tuple[str, datetime] | None:
    """Build (one snapshot), upload to a new path and sign the file, then
    record the link and move the row to ``emailing`` (fenced). The file of
    an earlier try or link (``old_path``) is deleted once the row points at
    the new one. Returns the link and its expiry, or ``None`` when this
    attempt no longer holds the row."""
    document = await account_export.build_snapshot(sessions, user_id)
    data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode()
    path = object_path(claim.id, utcnow())
    await storage.upload(path, data, CONTENT_TYPE)
    # Recorded at once, so a later failure (signing) can still delete it.
    if not await _fenced_update(
        sessions, claim, object_path=path, size_bytes=len(data)
    ):
        return await _lost(sessions, claim, path, storage)
    if old_path and old_path != path:
        try:
            await storage.delete([old_path])
        except StorageError:
            logger.warning(
                "account_export: could not delete a replaced file "
                "(export_id=%s); the sweep removes it",
                claim.id,
            )
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
        email_accepted_at=None,  # a new link: nothing sent with it yet
    ):
        return url, expires_at
    return await _lost(sessions, claim, path, storage)


async def _lost(
    sessions: Sessions, claim: Claim, path: str, storage: ObjectStorage
) -> None:
    """This attempt no longer holds the row. Its file (a path of its own) is
    nobody's, unless the row still points at it (another attempt took over
    after it was recorded; that one replaces and deletes it)."""
    if await _row_path(sessions, claim.id) != path:
        await storage.delete([path])


def idempotency_key(claim: Claim, expires_at: datetime) -> str:
    """Resend's key for the email of this link: the same on every retry with
    it, a new one for a newly signed link."""
    return f"account-export-{claim.id}-{int(expires_at.timestamp())}"


async def _send(
    sessions: Sessions,
    claim: Claim,
    message: email.Message,
    key: str,
    accepted: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    """Send the email once per link: the same key and the same payload on
    every retry, so Resend sends it once.

    ``accepted`` says whether an earlier try with this key may have been
    accepted (``email_accepted_at``). Only then does Resend's 409 for the
    key with another payload (the address changed meanwhile) mean the email
    went out; otherwise it is a failure, so a key Resend kept from a refused
    request never marks an unsent export as delivered. A timeout or a 5xx
    may have been accepted, so it sets the flag before the failure is
    raised."""
    try:
        await email.send(message, idempotency_key=key, transport=transport)
    except EmailError as exc:
        if exc.code == email.KEY_ALREADY_USED and accepted:
            logger.info(
                "account_export: email already sent under this key (export_id=%s)",
                claim.id,
            )
            return
        if exc.status is None or exc.status >= 500:
            await _fenced_update(sessions, claim, email_accepted_at=utcnow())
        raise


async def _mark_ready(sessions: Sessions, claim: Claim) -> bool:
    now = utcnow()
    return await _fenced_update(
        sessions,
        claim,
        status=STATUS_READY,
        email_accepted_at=now,
        completed_at=now,
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
    it). Raises on a storage, email or build failure.

    A recorded link with less than :data:`MIN_LINK_LEFT` left (the export sat
    unsent, e.g. while email was down) is never emailed: the attempt starts
    afresh with a new build, upload and link, under a new key."""
    async with sessions() as db:
        export = await db.get(AccountExport, claim.id)
        user = await db.get(User, export.user_id) if export else None
        if export is None or user is None:
            return False  # the account (and its row) went away
        to, locale, user_id = user.email, export.locale, user.id
        url, expires_at = export.delivery_url, export.expires_at
        old_path = export.object_path
        accepted = export.email_accepted_at is not None
        await db.rollback()
    if url is None or expires_at is None or expires_at - utcnow() < MIN_LINK_LEFT:
        if url is not None:
            logger.info(
                "account_export: recorded link about to expire, re-signing "
                "(export_id=%s)",
                claim.id,
            )
        prepared = await _prepare(sessions, claim, user_id, old_path, storage)
        if prepared is None:
            return False
        url, expires_at = prepared
        accepted = False
    await _send(
        sessions,
        claim,
        export_email.render(locale, to, url, expires_at),
        idempotency_key(claim, expires_at),
        accepted,
        email_transport,
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
