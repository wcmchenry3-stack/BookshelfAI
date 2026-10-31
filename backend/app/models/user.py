from sqlalchemy import Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import IdMixin, TimestampMixin


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # One row per mailbox, ignoring case (#705, migration 0022), so a
        # concurrent case-variant first sign-in loses with a unique violation
        # (app/auth/identity.py turns it into 409 account_conflict). The
        # case-sensitive unique constraint on ``email`` below is kept: the
        # index implies it.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )

    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    google_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    # Bumped to revoke every access and refresh token the user holds (#689);
    # tokens carry it as the ``tv`` claim. See app/auth/sessions.py.
    token_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
