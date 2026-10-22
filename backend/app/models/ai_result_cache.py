import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AIResultCache(Base):
    """One cached scan result per (user, normalised image, feature, model,
    prompt version) (#534).

    Written and read by ``app.ai.cache`` only. ``cache_key`` is the lowercase
    SHA-256 hex of the user id, the image hash, the feature, the model and the
    prompt version (see ``app.ai.cache.cache_key``), so it is always scoped to
    one user: there is no shared or cross-user entry.

    Holds the SHA-256 of the normalised JPEG (never the image bytes) and the
    validated candidate list (title, author, confidence, ISBN-13, ISBN-10;
    never the model's raw output). ~2-4 KB per row. Rows expire
    ``cache.ttl_days`` (default 30) after ``created_at``; expired rows are
    never served and are purged in bounded batches by the daily cleanup task.
    Each user keeps at most ``cache.max_rows_per_user`` rows (oldest evicted
    first). Rows go with the user (ON DELETE CASCADE). Phase 2 account
    deletion and export must include this table.
    """

    __tablename__ = "ai_result_cache"
    __table_args__ = (
        CheckConstraint(
            "char_length(cache_key) = 64 AND char_length(image_sha256) = 64",
            name="ck_ai_result_cache_hash_length",
        ),
        CheckConstraint("expires_at > created_at", name="ck_ai_result_cache_expiry"),
        # Per-user eviction (oldest first) and the ON DELETE CASCADE scan.
        Index("ix_ai_result_cache_user_created", "user_id", "created_at"),
        # The daily purge of expired rows.
        Index("ix_ai_result_cache_expires_at", "expires_at"),
    )

    # SHA-256 hex (64 lowercase characters).
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 hex of the normalised JPEG bytes sent to the provider.
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 hex of the request shape (``gateway.request_fingerprint``):
    # provider, prompt as rendered, schema, effort, max_tokens, PROMPT_VERSION.
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    # A JSON array of validated candidates; JSONB on Postgres, plain JSON
    # elsewhere so the model still loads on SQLite.
    result: Mapped[Any] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
