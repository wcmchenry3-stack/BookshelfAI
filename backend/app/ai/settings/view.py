"""``AISettings``: a validated, read-only view of the ``ai_settings`` rows."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from app.ai import settings as ai_settings
from app.ai.registry import FeatureSpec, FeatureSpecError


@dataclass(frozen=True)
class AISettings:
    """A validated view of ai_settings; missing keys use the env defaults."""

    rows: Mapping[str, Any]

    def get(self, key: str) -> Any:
        if key in self.rows:
            return self.rows[key]
        return ai_settings.DEFINITIONS[key].default()

    @property
    def scan_enabled(self) -> bool:
        return self.get(ai_settings.MASTER_SWITCH)

    @property
    def enhanced_scan_enabled(self) -> bool:
        return self.get("scan.enhanced.enabled")

    @property
    def max_image_pixels(self) -> int:
        return self.get("scan.max_image_pixels")

    @property
    def global_daily_limit(self) -> int:
        return self.get("scan.global_daily_limit")

    @property
    def free_daily_ceiling_micros(self) -> int:
        return self.get(ai_settings.FREE_DAILY_CEILING_KEY)

    @property
    def zero_result_threshold(self) -> int:
        return self.get(ai_settings.ZERO_RESULT_THRESHOLD_KEY)

    @property
    def zero_result_window_hours(self) -> int:
        return self.get(ai_settings.ZERO_RESULT_WINDOW_HOURS_KEY)

    @property
    def zero_result_action(self) -> str:
        return self.get(ai_settings.ZERO_RESULT_ACTION_KEY)

    @property
    def scan_records_enabled(self) -> bool:
        return self.get(ai_settings.SCAN_RECORDS_ENABLED_KEY)

    @property
    def scan_records_retention_days(self) -> int:
        return self.get(ai_settings.SCAN_RECORDS_RETENTION_DAYS_KEY)

    @property
    def scan_records_max_per_user(self) -> int:
        return self.get(ai_settings.SCAN_RECORDS_MAX_PER_USER_KEY)

    @property
    def scan_requests_per_user_daily(self) -> int:
        return self.get(ai_settings.SCAN_REQUESTS_PER_USER_DAILY_KEY)

    def daily_allowance(self, tier: str) -> int:
        """Found scans per UTC day for ``tier`` (``"free"`` or ``"premium"``):
        the unit every scan reserves before the provider (#577). There is no
        monthly allowance (#582)."""
        return self.get(ai_settings.found_key(tier))

    def no_match_allowance(self, tier: str) -> int:
        """No-match scans per UTC day for ``tier`` (#577)."""
        return self.get(ai_settings.no_match_key(tier))

    def feature_enabled(self, feature: str) -> bool:
        """``scan.standard.enabled`` is the master switch for every scan."""
        if feature not in ai_settings.SCAN_FEATURES:
            return True
        return self.scan_enabled and self.get(f"{feature}.enabled")

    def spec_for(self, spec: FeatureSpec) -> FeatureSpec:
        """The per-request spec: ``spec`` with the provider/model overrides and
        the kill switch applied. ``replace`` re-runs the registry's own
        validation, so the registry stays the allow-list authority."""
        if spec.name not in ai_settings.SCAN_FEATURES:
            return spec
        enabled = spec.enabled and self.feature_enabled(spec.name)
        provider = self.rows.get(f"{spec.name}.provider", spec.provider)
        model = self.rows.get(f"{spec.name}.model", spec.model)
        try:
            return replace(spec, provider=provider, model=model, enabled=enabled)
        except FeatureSpecError:
            ai_settings._warn_ignored(f"{spec.name}.model", "rejected by the registry")
            return replace(spec, enabled=enabled)
