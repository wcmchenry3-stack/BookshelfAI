import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    false,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

WINDOW_DAY = "day"
WINDOW_MONTH = "month"


class AIQuota(Base):
    """Per-user count of AI calls reserved, per quota key and window (#528).

    ``feature`` is the quota key: every scan, standard or enhanced, counts
    under ``"scan"`` (``app.ai.quota.SCAN_QUOTA_FEATURE``, one shared
    allowance); the column lets later AI features have their own.
    ``window_kind`` is ``day`` (``window_start`` = the UTC date) or ``month``
    (``window_start`` = the first day of the UTC calendar month). One small
    row (~70 bytes) per active user, quota key and window. Day rows are purged
    after 90 days and month rows after 13 months by the daily cleanup task
    (``app/ai/quota.py``). Rows go with the user (ON DELETE CASCADE).
    """

    __tablename__ = "ai_quota"
    __table_args__ = (
        CheckConstraint(
            "window_kind IN ('day', 'month')", name="ck_ai_quota_window_kind"
        ),
        CheckConstraint("units_used >= 0", name="ck_ai_quota_units_used"),
        # The global daily count and the purge both filter on these columns.
        Index("ix_ai_quota_window", "window_kind", "window_start"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    window_start: Mapped[date] = mapped_column(Date, primary_key=True)
    units_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


class AIInflight(Base):
    """At most one paid AI call in flight per user: a lease row (#528).

    Acquired by a conditional upsert that succeeds only when there is no row
    or its lease has expired; deleted (only by the holder, matched on
    ``lease_id``) when the call settles or is refunded. ``lease_expires_at``
    is the backstop for a crashed request. At most one row per user.
    """

    __tablename__ = "ai_inflight"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Identifies the holder, so a slow request whose lease expired and was
    # taken over cannot release the new holder's lease.
    lease_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Which scan units this lease's holder took (#590). Each flag is set in
    # the same transaction as its ai_quota increment and cleared in the same
    # transaction as its give-back, so a commit whose acknowledgement was lost
    # can be told apart afterwards and a unit is never given back twice. A
    # takeover of an expired lease resets them (they belong to the new lease).
    daily_taken: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    monthly_taken: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    enhanced_taken: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
