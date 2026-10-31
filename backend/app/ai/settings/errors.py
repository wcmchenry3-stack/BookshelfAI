"""The two errors of ``app.ai.settings``."""

from app.services.book_identifier import ScanUnavailableError


class AISettingValueError(ValueError):
    """A key is unknown or a value is outside its type, bounds or allow-list."""


class AISettingsUnavailableError(ScanUnavailableError):
    """ai_settings could not be read and nothing usable is cached (never
    read, or older than ``AI_SETTINGS_MAX_STALE_SECONDS``): fail closed.

    A ``ScanUnavailableError`` so that, if it surfaces inside ``run_feature``,
    the scan endpoint treats it like any provider outage (no provider was
    called, so the daily reservation is refunded).
    """
