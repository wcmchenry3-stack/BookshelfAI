import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

STATE_PENDING = "pending"
# The endpoint's write committed; no response stored (yet).
STATE_COMMITTED = "committed"
STATE_COMPLETE = "complete"

# The largest response body stored for a replay (#685), in bytes. Kept
# literal in migration 0033's check constraint; change both together.
MAX_RESPONSE_BODY_BYTES = 65_536


class IdempotencyKey(Base):
    """One ``Idempotency-Key`` a user sent on a write (#685), with the
    response to replay when the same request comes again.

    Written only by ``app.services.idempotency``. A row is ``pending`` while
    the first request runs, ``committed`` from the commit of its write (in
    that same transaction) and ``complete`` once its response is stored. A
    request that fails before committing deletes its row, so a retry runs
    again; a committed row is never deleted early, so a write never runs
    twice. Never replayed after 24 hours; the daily cleanup
    deletes older rows (``purge_expired``). ``user_id`` is ``ON DELETE
    CASCADE``: the stored body is the user's data and goes with the account.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        # One row per key per user; also the lookup, and the scan for the
        # cascade on account deletion (user_id leads).
        UniqueConstraint("user_id", "key", name="uq_idempotency_keys_user_key"),
        CheckConstraint(
            "state IN ('pending', 'committed', 'complete')",
            name="ck_idempotency_keys_state",
        ),
        CheckConstraint(
            f"response_body IS NULL OR octet_length(response_body) <= "
            f"{MAX_RESPONSE_BODY_BYTES}",
            name="ck_idempotency_keys_body_size",
        ),
        # The daily purge deletes by age.
        Index("ix_idempotency_keys_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The client's key: 1-255 printable ASCII characters (validated first).
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 (hex) of the method, path, If-Match and canonical JSON body.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    # The response to replay; NULL while pending. 204s have no body.
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Only ETag and Location (``idempotency.REPLAYED_HEADERS``).
    response_headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Set from the app's clock (the 24 h window and the pending lease are
    # measured against it); the server default is a fallback.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
