"""The signed-in user's own settings (#913).

``PATCH /users/me/preferences`` changes only the caller's row: the user comes
from the access token (``get_current_user``), never from the request. ``GET
/auth/me`` returns the same fields.
"""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.user import User
from app.schemas.auth import UserPreferences, UserPreferencesUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/users", tags=["users"])


@router.patch("/me/preferences", response_model=UserPreferences)
@limiter.limit(settings.rate_limit_writes)
async def update_preferences(
    request: Request,
    body: UserPreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Set the fields the body sends and leave the others as they are."""
    if "default_tab" in body.model_fields_set:
        current_user.default_tab = body.default_tab
    await db.commit()
    logger.info("Preferences updated (user_id=%s)", current_user.id)
    return current_user
