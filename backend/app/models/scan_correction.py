import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ScanCorrection(Base):
    """Whether the user changed the scan's reading when they saved an
    "Add it yourself" entry pre-filled from it (#631).

    One row per (scan record, candidate) the user saved, written by
    ``app.ai.scan_corrections`` only: three server-derived booleans, never
    any text (neither what the scan read nor what the user typed). The
    feature, tier and model are copied from the scan record. The unique
    (scan_record_id, candidate_index) makes a second save of the same
    candidate add nothing.

    Every row is counted into ``scan_corrections_daily`` in the same
    transaction that inserts it, so the daily totals never depend on this
    row surviving. Rows go with their scan record (ON DELETE CASCADE: the
    retention purge, the per-user cap and account deletion) and are deleted
    30 days after ``created_at`` by the daily cleanup task. The user is
    reachable through the scan record only. Phase 2 account deletion and
    export must include this table. ~60 bytes per row.
    """

    __tablename__ = "scan_corrections"
    __table_args__ = (
        UniqueConstraint(
            "scan_record_id",
            "candidate_index",
            name="uq_scan_corrections_record_candidate",
        ),
        CheckConstraint("candidate_index >= 0", name="ck_scan_corrections_index"),
        # The 30-day purge.
        Index("ix_scan_corrections_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scan_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scan_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    candidate_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # Copied from the scan record: the gateway feature, the user's tier and
    # the model that produced the candidate.
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    title_edited: Mapped[bool] = mapped_column(Boolean, nullable=False)
    author_edited: Mapped[bool] = mapped_column(Boolean, nullable=False)
    isbn_edited: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ScanCorrectionDaily(Base):
    """Daily totals of ``scan_corrections`` per (UTC day, feature, model)
    (#631), next to the cost per scan in ``ai_usage_daily``.

    Counts are added when a correction row is inserted (same transaction),
    so nothing is lost when the scan record and its corrections are deleted
    before 30 days. No user column. Kept 400 days, like ``ai_usage_daily``.
    """

    __tablename__ = "scan_corrections_daily"
    __table_args__ = (
        CheckConstraint(
            "prefilled_saves >= 0 AND title_edited >= 0 AND author_edited >= 0 "
            "AND isbn_edited >= 0 AND any_edited >= 0",
            name="ck_scan_corrections_daily_non_negative",
        ),
    )

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    prefilled_saves: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title_edited: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_edited: Mapped[int] = mapped_column(BigInteger, nullable=False)
    isbn_edited: Mapped[int] = mapped_column(BigInteger, nullable=False)
    any_edited: Mapped[int] = mapped_column(BigInteger, nullable=False)
