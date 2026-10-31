"""Validating writes and sanitising rows read from ``ai_settings``."""

from collections.abc import Mapping
from typing import Any

from app.ai import settings as ai_settings
from app.ai.settings.errors import AISettingValueError


def _pair_problems(rows: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(feature, message)`` for every provider/model pair not allowed.

    A missing provider or model is taken from the registry default.
    """
    problems = []
    for feature in ai_settings.SCAN_FEATURES:
        spec = ai_settings.get_feature(feature)
        provider = rows.get(f"{feature}.provider", spec.provider)
        model = rows.get(f"{feature}.model", spec.model)
        if model not in ai_settings.ALLOWED_MODELS.get(provider, frozenset()):
            # Both are known names here (validated per key), so safe to show.
            message = f"model {model!r} is not in the allow-list for {provider!r}"
            problems.append((feature, message))
    return problems


def validate_value(key: str, value: Any) -> Any:
    """Check one key's type and bounds (not the provider/model pairing)."""
    definition = ai_settings.DEFINITIONS.get(key)
    if definition is None:
        raise AISettingValueError(
            f"unknown key {key!r}; known keys: {', '.join(ai_settings.KNOWN_KEYS)}"
        )
    try:
        return definition.validate(value)
    except AISettingValueError as exc:
        raise AISettingValueError(f"{key} {exc}") from None


def validate_write(key: str, value: Any, current: Mapping[str, Any]) -> Any:
    """Validate ``key = value`` against the bounds and, for a provider or
    model, against the allow-list given the other half of the pair in
    ``current`` (already-validated rows). Returns the typed value."""
    typed = ai_settings.validate_value(key, value)
    for _feature, message in ai_settings._pair_problems({**current, key: typed}):
        raise AISettingValueError(f"{key}: {message}")
    return typed


def _warn_ignored(key: str, reason: str) -> None:
    # The stored value is not echoed: it came from outside the code.
    ai_settings.logger.warning(
        "ai_settings: ignoring %s (%s); using the default", key, reason
    )
    ai_settings.sentry_sdk.capture_message(
        f"ai_settings: ignoring invalid value for {key}", level="warning"
    )


def _is_switch(key: str) -> bool:
    return key.endswith(".enabled")


def _warn_switch_disabled(key: str, reason: str) -> None:
    # The stored value is not echoed: it came from outside the code.
    ai_settings.logger.warning(
        "ai_settings: invalid value for %s (%s); treated as disabled", key, reason
    )
    ai_settings.sentry_sdk.capture_message(
        f"ai_settings: invalid value for {key}; treated as disabled",
        level="warning",
    )


def carried_over(
    values: Mapping[str, Any], actors: Mapping[str, str | None]
) -> dict[str, Any]:
    """The ``found_daily`` values carried over from the retired
    ``quota.<tier>.daily`` rows (#577), given the stored ``values`` and who
    wrote each row (``actors``, the ``updated_by`` column).

    A tier's old value is carried when no ``found_daily`` row is stored, the
    old row was written by an operator (not :data:`SEED_ACTOR`: a seeded row
    is only the env default, and the new default applies) and it is valid.
    Never raises; an invalid old value is not carried (the default applies,
    without a warning: the key is retired)."""
    carried: dict[str, Any] = {}
    for tier in ai_settings.QUOTA_TIERS:
        old, new = ai_settings.legacy_daily_key(tier), ai_settings.found_key(tier)
        if new in values or old not in values:
            continue
        if actors.get(old) == ai_settings.SEED_ACTOR:
            continue
        try:
            carried[new] = ai_settings._daily_allowance(values[old])
        except AISettingValueError:
            continue
    return carried


def _sanitise_rows(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate rows read from the table.

    Unknown keys are dropped (retired keys, :data:`RETIRED_KEYS`, without a
    log line: they are expected until the contract migration). An invalid ``*.enabled`` value becomes ``False``
    (fail closed); any other invalid value is dropped, so the env/registry
    default is used.
    """
    valid: dict[str, Any] = {}
    for key, value in raw.items():
        definition = ai_settings.DEFINITIONS.get(key)
        if definition is None:
            if key not in ai_settings.RETIRED_KEYS:
                ai_settings.logger.info("ai_settings: ignoring unknown key %r", key)
            continue
        try:
            valid[key] = definition.validate(value)
        except AISettingValueError as exc:
            if ai_settings._is_switch(key):
                valid[key] = False
                ai_settings._warn_switch_disabled(key, str(exc))
            else:
                ai_settings._warn_ignored(key, str(exc))
    for feature, message in ai_settings._pair_problems(valid):
        model_key, provider_key = f"{feature}.model", f"{feature}.provider"
        if model_key in valid:
            del valid[model_key]
            ai_settings._warn_ignored(model_key, message)
        if ai_settings._pair_problems(valid) and provider_key in valid:
            # Even the registry's default model is not allowed for the stored
            # provider: fall back to the registry provider too.
            del valid[provider_key]
            ai_settings._warn_ignored(provider_key, message)
    return valid
