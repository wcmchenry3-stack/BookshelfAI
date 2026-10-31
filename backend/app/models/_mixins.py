"""Column mixins shared by the models (#814).

Each mixin declares one column definition that several tables repeat, so a
change is made once. They change nothing in the schema: every column keeps
its name, type, nullability, defaults, foreign key and index, and its place
in the table.

Column order: SQLAlchemy puts a mixin's columns after the class's own
columns unless ``sort_order`` says otherwise. ``id`` (-2) and ``user_id``
(-1) sort first, ``created_at`` (1) and ``updated_at`` (2) last, and the
daily key's ``day``, ``feature``, ``model`` (-3 to -1) first, which is where
every table using them declared them before the mixins existed.
``tests/unit/test_model_schema_snapshot.py`` pins the result.
"""

import uuid
from datetime import date, datetime
from typing import ClassVar

from sqlalchemy import Date, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column


class IdMixin:
    """A UUID primary key ``id``, generated client-side (``uuid4``)."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, sort_order=-2
    )


class UserOwnedMixin:
    """``user_id``: the owning user, required, deleted with the user
    (``ON DELETE CASCADE``). Set ``user_id_indexed = True`` for a
    single-column index on it (``ix_<table>_user_id``)."""

    user_id_indexed: ClassVar[bool] = False

    @declared_attr
    def user_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            # None, not False, when unindexed: the value the column had
            # before the mixin (the schema is the same either way).
            index=cls.user_id_indexed or None,
            sort_order=-1,
        )


class DailyFeatureModelKeyMixin:
    """The leading primary-key columns of a daily totals table: the UTC
    ``day``, the gateway ``feature`` and the ``model``. A table may add
    more key columns after them (``ai_usage_daily.status``)."""

    day: Mapped[date] = mapped_column(Date, primary_key=True, sort_order=-3)
    feature: Mapped[str] = mapped_column(String(64), primary_key=True, sort_order=-2)
    model: Mapped[str] = mapped_column(String(64), primary_key=True, sort_order=-1)


class CreatedAtMixin:
    """``created_at``: set by the database on insert."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, sort_order=1
    )


class TimestampMixin(CreatedAtMixin):
    """``created_at`` plus ``updated_at``, which the ORM sets on every
    update (``onupdate``)."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        sort_order=2,
    )
