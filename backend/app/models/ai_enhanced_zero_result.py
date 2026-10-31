import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, false, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import IdMixin, UserOwnedMixin


class AIEnhancedZeroResult(IdMixin, UserOwnedMixin, Base):
    """One row per uncharged enhanced scan that returned no books (#533).

    Written by ``app.ai.quota.release`` in the same transaction as the scan's
    settle (usage-ledger row, cost settle, lease delete), only when the
    ``scan.enhanced`` unit is given back because the scan found nothing. A
    charged zero-result scan (the user is at the threshold) writes no row.
    The per-user count inside ``scan.enhanced.zero_result_window_hours`` is
    compared with ``scan.enhanced.zero_result_threshold`` (``app/ai/quota/zero_result.py``).
    ``warned`` marks the row that sent the threshold warning.

    Holds no image, hash or content: the user, the time and the ledger call
    (``ai_usage.call_id``; no FK, because ledger rows are rolled up and
    deleted after 30 days). ~40 bytes per row. Rows older than 30 days are
    purged in bounded batches by the daily cleanup task; rows go with the user
    (ON DELETE CASCADE). Phase 2 account export must include this table.
    """

    __tablename__ = "ai_enhanced_zero_results"
    __table_args__ = (
        # The window count: WHERE user_id = ? AND created_at > ?.
        Index("ix_ai_enhanced_zero_results_user_created", "user_id", "created_at"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # ai_usage.call_id of the scan's provider call; NULL when the settle had
    # no ledger row.
    ledger_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    # True on the row whose insert sent the threshold warning. A new warning
    # is sent only while no such row of the user is inside the window, so at
    # most one warning per user per window.
    warned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
