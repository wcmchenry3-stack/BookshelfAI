from datetime import date

from sqlalchemy import BigInteger, CheckConstraint, Date, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AISpendDaily(Base):
    """Service-wide AI spend per UTC day: the cost circuit breaker (#531).

    One row per UTC day, for all tiers and features. ``reserved_micros`` is the
    estimate (``FeatureSpec.est_cost_ceiling_micros``) of the calls in flight;
    ``settled_micros`` is what finished calls cost (micro-dollars, 1e-6 USD).
    A Free-tier reservation succeeds only while ``reserved + settled + est``
    stays within ``breaker.free_daily_ceiling_micros``; Premium always adds.
    ``alerted_mask`` has one bit per alert threshold (50/75/90 %) so each
    alert is sent once per day. No user column and no content. Rows older
    than 90 days are purged by the daily cleanup task (``app/ai/breaker.py``).
    """

    __tablename__ = "ai_spend_daily"
    __table_args__ = (
        CheckConstraint(
            "reserved_micros >= 0 AND settled_micros >= 0 AND alerted_mask >= 0",
            name="ck_ai_spend_daily_non_negative",
        ),
    )

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    reserved_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    settled_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    alerted_mask: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
