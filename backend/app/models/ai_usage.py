import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

STATUS_OK = "ok"
STATUS_REFUSED = "refused"
STATUS_TRUNCATED = "truncated"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
STATUS_REFUNDED = "refunded"
STATUS_CACHE_HIT = "cache_hit"

USAGE_STATUSES = (
    STATUS_OK,
    STATUS_REFUSED,
    STATUS_TRUNCATED,
    STATUS_ERROR,
    STATUS_TIMEOUT,
    STATUS_REFUNDED,
    STATUS_CACHE_HIT,
)

_STATUS_IN = "status IN ({})".format(", ".join(f"'{s}'" for s in USAGE_STATUSES))


class AIUsage(Base):
    """One row per paid AI provider call: the usage ledger (#530).

    Written by ``app.ai.ledger`` in the same commit as the quota settle (the
    lease release). Holds counts and cost only: never the prompt, the image,
    or the model's output. ``input_tokens``, ``cache_read_tokens`` and
    ``cache_write_tokens`` are disjoint (see ``ProviderResult``);
    ``cost_micros`` is priced from ``app.ai.prices`` at ``price_version``.

    ``user_id`` is ``ON DELETE SET NULL``: deleting an account keeps the cost
    history without the personal link. Raw rows are kept 30 days, then rolled
    up into ``ai_usage_daily`` (no user) and deleted by the daily cleanup task.
    ~80 bytes per row.
    """

    __tablename__ = "ai_usage"
    __table_args__ = (
        CheckConstraint(_STATUS_IN, name="ck_ai_usage_status"),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0 AND cache_read_tokens >= 0 "
            "AND cache_write_tokens >= 0 AND cost_micros >= 0",
            name="ck_ai_usage_non_negative",
        ),
        # The rollup scans by age.
        Index("ix_ai_usage_created_at", "created_at"),
        # Per-user lookups, and the ON DELETE SET NULL scan on account delete.
        Index("ix_ai_usage_user_created", "user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    price_version: Mapped[str] = mapped_column(String(16), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # The server-generated X-Request-ID (app.core.logging); NULL outside a
    # request.
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AIUsageDaily(Base):
    """Daily totals of ``ai_usage`` per (UTC day, feature, model, status) (#530).

    Upserted (counts added) by the rollup when raw rows pass 30 days; no user
    column. Kept 400 days. Tens of rows per day.
    """

    __tablename__ = "ai_usage_daily"
    __table_args__ = (
        CheckConstraint(_STATUS_IN, name="ck_ai_usage_daily_status"),
        CheckConstraint(
            "calls >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND cost_micros >= 0",
            name="ck_ai_usage_daily_non_negative",
        ),
    )

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), primary_key=True)
    calls: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
