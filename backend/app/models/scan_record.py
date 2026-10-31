from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    String,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import IdMixin, UserOwnedMixin

# JSONB on Postgres, plain JSON elsewhere so the model still loads on SQLite.
# ``none_as_null`` makes Python ``None`` a SQL NULL (the default would store
# JSON ``null``, which ``IS NOT NULL`` and ck_scan_records_steps treat as a value).
_JSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class ScanRecord(IdMixin, UserOwnedMixin, Base):
    """One scan record per (user, photo fingerprint) (#585, decision #573).

    Written and read by ``app.ai.scan_records`` only. ``fingerprint`` is the
    SHA-256 hex of the normalised JPEG (the bytes the provider would get), so
    only the exact same photo matches; it is never derived from the request.
    The record is keyed on the user and the fingerprint only, not on the
    prompt or model: an old answer stays valid when either changes.

    It holds the provider's validated candidate list (``candidates``; never
    the model's raw output), the enrichment result (``enrichment``: the Open
    Library / Google Books book data, without the per-user library flag,
    which is re-checked on every scan), which steps finished (``ai_done``,
    ``enrich_done``) and the feature, tier and model that produced the
    answer. Never image bytes. Usually 1-10 KB (the enrichment holds book
    descriptions).

    Rows are deleted ``scan_records.retention_days`` (default 30) after
    ``created_at`` by the daily cleanup task, are never served past that
    age, and go with the user (ON DELETE CASCADE). Each user keeps at most
    ``scan_records.max_per_user`` rows (oldest evicted first). Phase 2
    account deletion and export must include this table.
    """

    __tablename__ = "scan_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "fingerprint", name="uq_scan_records_user_fingerprint"
        ),
        CheckConstraint(
            "char_length(fingerprint) = 64", name="ck_scan_records_fingerprint"
        ),
        CheckConstraint(
            "NOT enrich_done OR (ai_done AND enrichment IS NOT NULL)",
            name="ck_scan_records_steps",
        ),
        # Per-user eviction (oldest first) and the ON DELETE CASCADE scan.
        Index("ix_scan_records_user_created", "user_id", "created_at"),
        # The daily retention purge.
        Index("ix_scan_records_created_at", "created_at"),
    )

    # SHA-256 hex of the normalised JPEG bytes.
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # The gateway feature (scan.standard / scan.enhanced), the user's tier and
    # the model that produced ``candidates``.
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    tier: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # A JSON array of validated candidates (title, author, confidence, ISBNs).
    candidates: Mapped[Any] = mapped_column(_JSON, nullable=False)
    # A JSON array of enriched books, or NULL until enrichment finishes.
    enrichment: Mapped[Any | None] = mapped_column(_JSON, nullable=True)
    ai_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    enrich_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
