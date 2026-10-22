from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AISetting(Base):
    """One server-side AI dial or kill switch (``app/ai/settings.py``).

    A handful of rows (one per key), each a small JSON value. The keys, their
    types and bounds live in code; anything else in the table is ignored. Rows
    are seeded from the env at startup and changed only with the CLI
    (``python -m app.ai.settings``) or directly in the database, never from a
    request.
    """

    __tablename__ = "ai_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    # JSONB on Postgres; plain JSON elsewhere so the model still loads on SQLite.
    value: Mapped[Any] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)
