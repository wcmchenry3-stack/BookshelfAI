import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ScanUsageDaily(Base):
    """Per-user count of scans that reached the vision provider, per UTC day.

    One small row per active user per day (~40 bytes of data). Rows older than
    90 days are purged by the daily cleanup task (app/services/scan_quota.py).
    """

    __tablename__ = "scan_usage_daily"
    __table_args__ = (Index("ix_scan_usage_daily_usage_date", "usage_date"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    scans: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
