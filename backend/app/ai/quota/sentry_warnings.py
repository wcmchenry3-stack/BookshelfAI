"""The quota's Sentry warnings (no user id, image, hash or content)."""

import logging

from app.ai import alerts, quota
from app.ai.entitlements import Tier
from app.ai.quota.constants import ENHANCED_FEATURE_TAG, FLAG_DAILY
from app.ai.quota.types import _Unit

logger = logging.getLogger(__package__)


def _tier_tag(tier: object) -> str:
    return tier.value if isinstance(tier, Tier) else "unknown"


def send_no_match_ceiling_warning(*, tier: object, ceiling: int) -> None:
    """One Sentry warning: a user's no-match scans reached the daily ceiling
    (#577). Only the tier and the ceiling: no user id, image, hash or
    content (emptied, stripped scopes, :mod:`app.ai.alerts`). Never
    raises."""
    try:
        alerts.capture_service_warning(
            "ai-quota: a user's no-match scans reached the daily ceiling",
            fingerprint=["ai-no-match-ceiling"],
            tags={"tier": quota._tier_tag(tier)},
            extras={"ceiling": ceiling},
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the no-match ceiling warning")


def send_zero_result_warning(
    *, count: int, threshold: int, window_hours: int, action: str
) -> None:
    """One Sentry warning: a user's uncharged zero-result enhanced scans
    reached the threshold. No user id, image, hash or content: the event is
    captured in emptied, stripped scopes (:mod:`app.ai.alerts`). Never
    raises."""
    try:
        alerts.capture_service_warning(
            "ai-enhanced: zero-result enhanced scans reached the threshold",
            fingerprint=["ai-enhanced-zero-results", action],
            tags={"feature": ENHANCED_FEATURE_TAG, "action": action},
            extras={
                "count": count,
                "threshold": threshold,
                "window_hours": window_hours,
            },
        )
        logger.warning(
            "ai_quota: zero-result enhanced scans reached the threshold "
            "(count=%d, window=%dh, action=%s)",
            count,
            window_hours,
            action,
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the zero-result warning")


_UNIT_TAGS = {
    FLAG_DAILY: "daily",
}


def send_unit_kept_warning(unit: _Unit, reason: str) -> None:
    """One Sentry warning and one log line: a unit this request attempted to
    take is left charged. Only the unit and the reason: no user id, email,
    lease id or content (emptied, stripped scopes, :mod:`app.ai.alerts`).
    Never raises, never changes the give-back's outcome."""
    unit_tag = _UNIT_TAGS.get(unit.flag, "unknown")
    try:
        alerts.capture_service_warning(
            "ai-quota: a reserved unit could not be given back",
            fingerprint=["ai-quota-unit-kept", reason],
            tags={"unit": unit_tag, "reason": reason},
            extras={"unit": unit_tag, "reason": reason},
        )
    except Exception:  # a warning must never fail a request
        logger.exception("ai_quota: could not send the unit-kept warning")
    logger.warning("ai_quota: unit kept (unit=%s, reason=%s)", unit_tag, reason)
