from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import CreatedAtMixin, IdMixin, UserOwnedMixin

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
# The file is stored and its link signed and recorded; the email may have
# gone out. From here a retry re-sends the same email and the file is kept
# until ``expires_at``.
STATUS_EMAILING = "emailing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
EXPORT_STATUSES = (
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_EMAILING,
    STATUS_READY,
    STATUS_FAILED,
)
OPEN_STATUSES = (STATUS_PENDING, STATUS_RUNNING, STATUS_EMAILING)
_STATUS_IN = "status IN ({})".format(", ".join(f"'{s}'" for s in EXPORT_STATUSES))


class AccountExport(IdMixin, UserOwnedMixin, CreatedAtMixin, Base):
    """One request for a copy of the user's data (#694).

    ``POST /account/export`` inserts a ``pending`` row; the export worker
    (``app/services/account_export_jobs.py``) claims it (``running``, with
    ``claimed_at`` as a lease and ``attempts`` counted), writes the JSON
    document to private storage at ``object_path``, signs its link and
    records it with ``expires_at`` (when the link stops working, 7 days after
    it was signed) as ``emailing``, then emails the link and marks it
    ``ready`` with ``completed_at``. A retry of an ``emailing`` row re-sends
    the same email from the recorded link (Resend dedupes it by its
    idempotency key). A job that keeps failing ends ``failed`` with
    ``error_code`` (a fixed code, never a message). Rows survive a restart:
    an open row (``pending``, ``running``, ``emailing``) whose lease ran out
    is picked up again.

    ``delivery_url`` is the signed link itself, a bearer credential for the
    file until ``expires_at``. It is stored so a retry sends a byte-identical
    email. It grants nothing a database reader does not already have (the
    file is a copy of rows in this database), stops working after 7 days,
    and is never logged or exported.

    The newest row inside 24 hours enforces the one-export-a-day cap. The
    daily cleanup deletes the object, then the row, once ``expires_at`` has
    passed (or 7 days after ``created_at`` for a row that never got one).
    ``user_id`` is ``ON DELETE CASCADE``; the object itself is not, so
    account deletion (#291) must delete it first, and the cleanup's storage
    sweep removes any object older than 8 days that no row points at. ~150 bytes per
    row; at most one row per user per day, each kept 7 days.
    """

    __tablename__ = "account_exports"
    __table_args__ = (
        CheckConstraint(_STATUS_IN, name="ck_account_exports_status"),
        CheckConstraint("attempts >= 0", name="ck_account_exports_attempts"),
        # The per-user cap (newest first) and the ON DELETE CASCADE scan.
        Index("ix_account_exports_user_created", "user_id", "created_at"),
        # The worker's queue: unfinished rows only, oldest first.
        Index(
            "ix_account_exports_open",
            "created_at",
            postgresql_where=text("status IN ('pending', 'running', 'emailing')"),
        ),
        # The daily purge.
        Index("ix_account_exports_expires_at", "expires_at"),
    )

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=STATUS_PENDING
    )
    # The language of the email (the app's locale when the user asked).
    locale: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # When the current run claimed the row; a run is presumed dead after the
    # lease (``account_export_jobs.LEASE``).
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The object's path inside ``settings.export_bucket``; set once uploaded.
    object_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The signed link (a credential until expires_at; never logged).
    delivery_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # When Resend accepted the email of the recorded link, or may have (a
    # timeout or a 5xx). Only then does its 409 for the key count as sent.
    email_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # When the signed link expires (and the purge may delete the object).
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Why the last attempt failed: a fixed code from account_export_jobs.
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
