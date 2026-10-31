"""The settings registry: every key, its validator, default and help text.

The registry is data: one tuple per area, in display order. Defaults are
read at call time through the package (``ai_settings.settings`` is the app's
``app.core.config.settings``; ``ai_settings.get_feature`` is the AI
registry's), so the env and a patch on the package both reach them.
"""

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.ai import settings as ai_settings
from app.ai.settings.constants import (
    DAILY_ALLOWANCE_BOUNDS,
    FREE_DAILY_CEILING_KEY,
    FREE_DAILY_CEILING_MICROS_BOUNDS,
    GLOBAL_DAILY_LIMIT_BOUNDS,
    MASTER_SWITCH,
    MAX_IMAGE_PIXELS_BOUNDS,
    QUOTA_TIERS,
    SCAN_FEATURES,
    SCAN_RECORDS_ENABLED_KEY,
    SCAN_RECORDS_MAX_PER_USER_BOUNDS,
    SCAN_RECORDS_MAX_PER_USER_KEY,
    SCAN_RECORDS_RETENTION_DAYS_BOUNDS,
    SCAN_RECORDS_RETENTION_DAYS_KEY,
    SCAN_REQUESTS_PER_USER_DAILY_BOUNDS,
    SCAN_REQUESTS_PER_USER_DAILY_KEY,
    ZERO_RESULT_ACTION_KEY,
    ZERO_RESULT_ACTIONS,
    ZERO_RESULT_THRESHOLD_BOUNDS,
    ZERO_RESULT_THRESHOLD_KEY,
    ZERO_RESULT_WINDOW_HOURS_BOUNDS,
    ZERO_RESULT_WINDOW_HOURS_KEY,
)
from app.ai.settings.validators import (
    _BOOL_BOUNDS,
    _bool,
    _daily_allowance,
    _free_daily_ceiling,
    _global_daily_limit,
    _max_image_pixels,
    _model,
    _provider,
    _range,
    _scan_records_max_per_user,
    _scan_records_retention_days,
    _scan_requests_per_user_daily,
    _zero_result_action,
    _zero_result_threshold,
    _zero_result_window_hours,
    found_key,
    no_match_key,
)


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    validate: Callable[[Any], Any]
    # The env/registry value, read at call time: the seed, and the fallback
    # when the row is missing or invalid.
    default: Callable[[], Any]
    description: str
    # Human-readable allowed values, shown by ``list`` (#581). The validator
    # stays the authority; this is for the operator's eyes.
    bounds: str = ""


_SWITCHES = (
    SettingDefinition(
        MASTER_SWITCH,
        _bool,
        lambda: ai_settings.settings.scan_enabled,
        "all scanning on/off (seed: SCAN_ENABLED)",
        bounds=_BOOL_BOUNDS,
    ),
    SettingDefinition(
        "scan.enhanced.enabled",
        _bool,
        lambda: ai_settings.settings.scan_enhanced_enabled,
        "enhanced scans on/off (seed: SCAN_ENHANCED_ENABLED)",
        bounds=_BOOL_BOUNDS,
    ),
)

# Per scan feature: its provider, then its model.
_PROVIDERS_AND_MODELS = tuple(
    definition
    for feature in SCAN_FEATURES
    for definition in (
        SettingDefinition(
            f"{feature}.provider",
            _provider,
            lambda f=feature: ai_settings.get_feature(f).provider,
            f"{feature} provider (seed: registry default)",
            bounds="a provider in ALLOWED_MODELS",
        ),
        SettingDefinition(
            f"{feature}.model",
            _model,
            lambda f=feature: ai_settings.get_feature(f).model,
            f"{feature} model (seed: registry default from env)",
            bounds="a model in the provider's allow-list",
        ),
    )
)

_UPLOADS_AND_GLOBAL = (
    SettingDefinition(
        "scan.max_image_pixels",
        _max_image_pixels,
        lambda: ai_settings.settings.scan_max_image_pixels,
        "decoded pixel cap for /scan uploads (seed: SCAN_MAX_IMAGE_PIXELS)",
        bounds=_range(MAX_IMAGE_PIXELS_BOUNDS),
    ),
    SettingDefinition(
        "scan.global_daily_limit",
        _global_daily_limit,
        lambda: ai_settings.settings.scan_global_daily_limit,
        "scans per UTC day across all users (seed: SCAN_GLOBAL_DAILY_LIMIT)",
        bounds=_range(GLOBAL_DAILY_LIMIT_BOUNDS),
    ),
)

# Per tier: its found allowance, then its no-match allowance (#577).
_ALLOWANCES = tuple(
    definition
    for tier in QUOTA_TIERS
    for definition in (
        SettingDefinition(
            found_key(tier),
            _daily_allowance,
            lambda t=tier: getattr(ai_settings.settings, f"scan_{t}_daily_limit"),
            f"{tier} scans per user per UTC day that found books, standard "
            f"and enhanced combined; also the units a scan needs to start "
            f"(seed: SCAN_{tier.upper()}_DAILY_LIMIT)",
            bounds=_range(DAILY_ALLOWANCE_BOUNDS),
        ),
        SettingDefinition(
            no_match_key(tier),
            _daily_allowance,
            lambda t=tier: getattr(
                ai_settings.settings, f"scan_{t}_no_match_daily_limit"
            ),
            f"{tier} standard scans per user per UTC day where the model "
            f"answered but found no book, not counted as a found scan "
            f"(seed: SCAN_{tier.upper()}_NO_MATCH_DAILY_LIMIT)",
            bounds=_range(DAILY_ALLOWANCE_BOUNDS),
        ),
    )
)

_SPEND_BREAKER = (
    SettingDefinition(
        FREE_DAILY_CEILING_KEY,
        _free_daily_ceiling,
        lambda: ai_settings.settings.scan_free_daily_ceiling_micros,
        "AI spend per UTC day in micro-dollars, all tiers counted, above "
        "which Free-tier scans pause (seed: SCAN_FREE_DAILY_CEILING_MICROS)",
        bounds=_range(FREE_DAILY_CEILING_MICROS_BOUNDS),
    ),
)

_ENHANCED_ZERO_RESULT = (
    SettingDefinition(
        ZERO_RESULT_THRESHOLD_KEY,
        _zero_result_threshold,
        lambda: ai_settings.DEFAULT_ZERO_RESULT_THRESHOLD,
        "zero-result enhanced scans recorded per user per window before "
        "the action applies (seed: code default 3)",
        bounds=_range(ZERO_RESULT_THRESHOLD_BOUNDS),
    ),
    SettingDefinition(
        ZERO_RESULT_WINDOW_HOURS_KEY,
        _zero_result_window_hours,
        lambda: ai_settings.DEFAULT_ZERO_RESULT_WINDOW_HOURS,
        "hours the zero-result count covers (seed: code default 24)",
        bounds=_range(ZERO_RESULT_WINDOW_HOURS_BOUNDS),
    ),
    SettingDefinition(
        ZERO_RESULT_ACTION_KEY,
        _zero_result_action,
        lambda: ai_settings.DEFAULT_ZERO_RESULT_ACTION,
        "at the threshold: charge (stop recording; every scan uses the "
        "daily unit) or throttle (refuse enhanced scans until the window "
        "clears) (seed: charge)",
        bounds=" or ".join(ZERO_RESULT_ACTIONS),
    ),
)

_SCAN_RECORDS = (
    SettingDefinition(
        SCAN_RECORDS_ENABLED_KEY,
        _bool,
        lambda: ai_settings.DEFAULT_SCAN_RECORDS_ENABLED,
        "per-user scan records on/off; off, every scan takes the normal "
        "path; an invalid value disables them (seed: code default true)",
        bounds=_BOOL_BOUNDS,
    ),
    SettingDefinition(
        SCAN_RECORDS_RETENTION_DAYS_KEY,
        _scan_records_retention_days,
        lambda: ai_settings.DEFAULT_SCAN_RECORDS_RETENTION_DAYS,
        "days a scan record is kept and served, then purged (seed: code default 30)",
        bounds=_range(SCAN_RECORDS_RETENTION_DAYS_BOUNDS),
    ),
    SettingDefinition(
        SCAN_RECORDS_MAX_PER_USER_KEY,
        _scan_records_max_per_user,
        lambda: ai_settings.DEFAULT_SCAN_RECORDS_MAX_PER_USER,
        "scan records kept per user, oldest evicted first " "(seed: code default 500)",
        bounds=_range(SCAN_RECORDS_MAX_PER_USER_BOUNDS),
    ),
)

_REQUEST_LIMIT = (
    SettingDefinition(
        SCAN_REQUESTS_PER_USER_DAILY_KEY,
        _scan_requests_per_user_daily,
        lambda: ai_settings.DEFAULT_SCAN_REQUESTS_PER_USER_DAILY,
        "/scan requests per user per UTC day, new photo or repeat, "
        "refused or not (seed: code default 100)",
        bounds=_range(SCAN_REQUESTS_PER_USER_DAILY_BOUNDS),
    ),
)

# Registry order (the CLI's ``choices``, seeding order, ``KNOWN_KEYS``).
_DEFINITION_TABLE: tuple[SettingDefinition, ...] = (
    *_SWITCHES,
    *_PROVIDERS_AND_MODELS,
    *_UPLOADS_AND_GLOBAL,
    *_ALLOWANCES,
    *_SPEND_BREAKER,
    *_ENHANCED_ZERO_RESULT,
    *_SCAN_RECORDS,
    *_REQUEST_LIMIT,
)


def _definitions() -> MappingProxyType[str, SettingDefinition]:
    """The registry by key, in registry order."""
    return MappingProxyType({d.key: d for d in _DEFINITION_TABLE})


DEFINITIONS: MappingProxyType[str, SettingDefinition] = _definitions()
KNOWN_KEYS = tuple(DEFINITIONS)
