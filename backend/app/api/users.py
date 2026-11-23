"""The signed-in user's own settings (#913) and AI consent (#1023).

``PATCH /users/me/preferences`` and ``POST`` / ``DELETE /users/me/ai-consent``
change only the caller's row: the user comes
from the access token (``get_current_user``), never from the request. ``GET
/auth/me`` returns the same fields.
"""

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._idempotency import IdempotencyRoute, idempotent
from app.auth.dependencies import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.user import User
from app.schemas.auth import AiConsentRead, UserPreferences, UserPreferencesUpdate
from app.services import ai_consent

logger = logging.getLogger(__name__)
# Every write here honours Idempotency-Key (#685).
router = APIRouter(prefix="/users", tags=["users"], route_class=IdempotencyRoute)


@router.patch("/me/preferences", response_model=UserPreferences)
@limiter.limit(settings.rate_limit_writes)
@idempotent
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


# The consent columns, re-read under the row lock below.
_CONSENT_COLUMNS = ["ai_consent_at", "ai_consent_withdrawn_at"]


async def _locked_consent(db: AsyncSession, user: User) -> None:
    """Re-read the consent columns with the user's row locked, so a grant
    and a withdrawal sent at once (two devices) apply one after the other."""
    await db.refresh(
        user, attribute_names=_CONSENT_COLUMNS, with_for_update={"key_share": True}
    )


@router.post("/me/ai-consent", response_model=AiConsentRead)
@limiter.limit(settings.rate_limit_writes)
@idempotent
async def grant_ai_consent(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Record that the user agreed to scan photos going to the AI provider
    (#1023). No body: the time is the server's. Idempotent: consent that
    already stands keeps its first time."""
    await _locked_consent(db, current_user)
    changed = ai_consent.grant(current_user)
    await db.commit()
    if changed:
        logger.info("AI consent given (user_id=%s)", current_user.id)
    return current_user


@router.delete("/me/ai-consent", response_model=AiConsentRead)
@limiter.limit(settings.rate_limit_writes)
@idempotent
async def withdraw_ai_consent(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Withdraw consent (#1023), effective at once: the app asks again at the
    next scan, and with AI_CONSENT_REQUIRED on /scan refuses until then.
    Idempotent: without consent nothing changes."""
    await _locked_consent(db, current_user)
    changed = ai_consent.withdraw(current_user)
    await db.commit()
    if changed:
        logger.info("AI consent withdrawn (user_id=%s)", current_user.id)
    return current_user
