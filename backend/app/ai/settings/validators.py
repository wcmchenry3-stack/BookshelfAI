"""Per-key validators and key names of ``app.ai.settings``."""

from collections.abc import Callable
from typing import Any

from app.ai import settings as ai_settings
from app.ai.settings.constants import (
    DAILY_ALLOWANCE_BOUNDS,
    FREE_DAILY_CEILING_MICROS_BOUNDS,
    GLOBAL_DAILY_LIMIT_BOUNDS,
    MAX_IMAGE_PIXELS_BOUNDS,
    SCAN_RECORDS_MAX_PER_USER_BOUNDS,
    SCAN_RECORDS_RETENTION_DAYS_BOUNDS,
    SCAN_REQUESTS_PER_USER_DAILY_BOUNDS,
    ZERO_RESULT_THRESHOLD_BOUNDS,
    ZERO_RESULT_WINDOW_HOURS_BOUNDS,
)
from app.ai.settings.errors import AISettingValueError


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise AISettingValueError("must be a JSON boolean (true or false)")
    return value


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in ai_settings.ALLOWED_MODELS:
        raise AISettingValueError(
            f"must be a known provider: {', '.join(sorted(ai_settings.ALLOWED_MODELS))}"
        )
    return value


def _model(value: Any) -> str:
    # The provider-specific check needs the paired provider; see _pair_problems.
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= ai_settings.MAX_MODEL_NAME_LENGTH
        or value not in ai_settings._ALL_MODELS
    ):
        raise AISettingValueError(
            "must be an allow-listed model: "
            f"{', '.join(sorted(ai_settings._ALL_MODELS))}"
        )
    return value


def _bounded_int(bounds: tuple[int, int]) -> Callable[[Any], int]:
    low, high = bounds

    def validate(value: Any) -> int:
        # bool is an int subclass; never accept it as a number.
        if isinstance(value, bool) or not isinstance(value, int):
            raise AISettingValueError("must be a JSON integer")
        if not low <= value <= high:
            raise AISettingValueError(f"must be between {low:,} and {high:,}")
        return value

    return validate


_max_image_pixels = _bounded_int(MAX_IMAGE_PIXELS_BOUNDS)
_global_daily_limit = _bounded_int(GLOBAL_DAILY_LIMIT_BOUNDS)
_daily_allowance = _bounded_int(DAILY_ALLOWANCE_BOUNDS)
_free_daily_ceiling = _bounded_int(FREE_DAILY_CEILING_MICROS_BOUNDS)
_zero_result_threshold = _bounded_int(ZERO_RESULT_THRESHOLD_BOUNDS)
_zero_result_window_hours = _bounded_int(ZERO_RESULT_WINDOW_HOURS_BOUNDS)
_scan_records_retention_days = _bounded_int(SCAN_RECORDS_RETENTION_DAYS_BOUNDS)
_scan_records_max_per_user = _bounded_int(SCAN_RECORDS_MAX_PER_USER_BOUNDS)
_scan_requests_per_user_daily = _bounded_int(SCAN_REQUESTS_PER_USER_DAILY_BOUNDS)


def found_key(tier: str) -> str:
    """The found-scans allowance key of ``tier`` (#577)."""
    return f"quota.{tier}.found_daily"


def no_match_key(tier: str) -> str:
    """The no-match allowance key of ``tier`` (#577)."""
    return f"quota.{tier}.no_match_daily"


def legacy_daily_key(tier: str) -> str:
    """The retired allowance key of ``tier`` (#577: now ``found_daily``)."""
    return f"quota.{tier}.daily"


def _zero_result_action(value: Any) -> str:
    if not isinstance(value, str) or value not in ai_settings.ZERO_RESULT_ACTIONS:
        raise AISettingValueError(
            f"must be one of: {', '.join(ai_settings.ZERO_RESULT_ACTIONS)}"
        )
    return value


def _range(bounds: tuple[int, int]) -> str:
    return f"{bounds[0]:,}..{bounds[1]:,}"


_BOOL_BOUNDS = "true or false"
