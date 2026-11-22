from sqlalchemy import CheckConstraint, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models._mixins import IdMixin, TimestampMixin

# The screens the app can open on (#913). ``users.default_tab`` holds one of
# these or NULL, which means Scan. The API schema (app/schemas/auth.py) and
# the app (frontend/lib/defaultTab.ts) use the same values.
DEFAULT_TABS = ("scan", "wishlist", "my-books")


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # One row per mailbox, ignoring case (#705, migration 0022), so a
        # concurrent case-variant first sign-in loses with a unique violation
        # (app/auth/identity.py turns it into 409 account_conflict). The
        # case-sensitive unique constraint on ``email`` below is kept: the
        # index implies it.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
        CheckConstraint(
            f"default_tab IS NULL OR default_tab IN {DEFAULT_TABS}",
            name="ck_users_default_tab",
        ),
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
    # The user's "Open on" screen (#913): one of DEFAULT_TABS, or NULL for
    # the default (Scan). Set only through PATCH /users/me/preferences.
    default_tab: Mapped[str | None] = mapped_column(String, nullable=True)
