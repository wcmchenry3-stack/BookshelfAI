"""Consent to send scan photos to the AI provider (#1023, Apple 5.1.2(i)).

The state lives on the user's row: ``users.ai_consent_at`` (last given) and
``users.ai_consent_withdrawn_at`` (last withdrawn); ``User.ai_consent`` says
whether it stands. Timestamps come from the server's clock only.

Rollout A: the app records consent first; ``POST /scan`` refuses without it
only while ``AI_CONSENT_REQUIRED`` is on (default off).
"""

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status

from app.core.config import settings
from app.models.user import User

# The 403 ``detail`` the app turns into the consent prompt.
AI_CONSENT_REQUIRED = "ai_consent_required"

_TICK = timedelta(microseconds=1)


def _after(earlier: datetime | None) -> datetime:
    """Now, but always later than ``earlier``: if the server clock stepped
    back, a new grant (or withdrawal) must still count as the latest."""
    now = datetime.now(UTC)
    return now if earlier is None or now > earlier else earlier + _TICK


def grant(user: User) -> bool:
    """Record consent now. A no-op when it already stands, so the first
    consent time is kept. Returns whether the row changed."""
    if user.ai_consent:
        return False
    user.ai_consent_at = _after(user.ai_consent_withdrawn_at)
    return True


def withdraw(user: User) -> bool:
    """Record a withdrawal now. A no-op when there is no consent to withdraw,
    so the last withdrawal time is kept. Returns whether the row changed."""
    if not user.ai_consent:
        return False
    user.ai_consent_withdrawn_at = _after(user.ai_consent_at)
    return True


def require_for_scan(user: User) -> None:
    """``POST /scan``'s check, run after the kill switches and before the
    request count, the allowance or the provider, so a refusal costs
    nothing. Only while ``AI_CONSENT_REQUIRED`` is on."""
    if settings.ai_consent_required and not user.ai_consent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=AI_CONSENT_REQUIRED
        )
