"""Server-side subscription tier.

The tier is decided here and only here. ``tier_for`` takes the database session
and the ORM user row, never the request, so no form field, header, query
parameter or JWT claim can reach it. Everyone is Free until subscriptions
exist (Phase 2.5); that story will read a server-verified entitlement from the
database inside this function.

Tests that need Premium patch ``tier_for`` (``monkeypatch.setattr``); there is
no flag, setting or route that does the same.
"""

import enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User


class Tier(enum.Enum):
    FREE = "free"
    PREMIUM = "premium"


async def tier_for(db: AsyncSession, user: User) -> Tier:
    """Return the user's tier. Always ``Tier.FREE`` for now."""
    return Tier.FREE
