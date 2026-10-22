"""DB-backed AI dials and kill switches (table ``ai_settings``).

The keys, their types and bounds are fixed here; the values live in the
database so they can change without a release:

==========================  =======  =========================================
key                         type     meaning
==========================  =======  =========================================
``scan.standard.enabled``   bool     false switches off ALL scanning (503
                                     ``scan_disabled``), as ``SCAN_ENABLED`` did
``scan.enhanced.enabled``   bool     false switches off enhanced scans only
                                     (503 ``enhanced_scan_disabled``)
``scan.standard.provider``  str      a provider in ``ALLOWED_MODELS``
``scan.enhanced.provider``  str      a provider in ``ALLOWED_MODELS``
``scan.standard.model``     str      a model in the provider's allow-list
``scan.enhanced.model``     str      a model in the provider's allow-list
``scan.max_image_pixels``   int      1,000,000..100,000,000 decoded pixels
``scan.global_daily_limit`` int      1..1,000,000 scans per UTC day, all users
                                     (503 ``scan_capacity``)
``quota.free.daily``        int      0..1,000 scans per user per UTC day
``quota.free.monthly``      int      0..100,000 scans per user per UTC month
``quota.premium.daily``     int      0..1,000 (as above, Premium tier)
``quota.premium.monthly``   int      0..100,000 (as above, Premium tier)
==========================  =======  =========================================

The ``quota.*`` allowances are per tier and apply to each scan feature
separately (``scan.standard`` and ``scan.enhanced`` each have their own
counters in ``ai_quota``); see ``app/ai/quota.py``.

**Seeding.** At startup :func:`seed_ai_settings_at_startup` inserts every key
that is missing, from the env (``SCAN_ENABLED``, ``SCAN_ENHANCED_ENABLED``,
``SCAN_MAX_IMAGE_PIXELS``, ``SCAN_GLOBAL_DAILY_LIMIT``, ``SCAN_FREE_DAILY_LIMIT``,
``SCAN_FREE_MONTHLY_LIMIT``, ``SCAN_PREMIUM_DAILY_LIMIT``,
``SCAN_PREMIUM_MONTHLY_LIMIT``) and the registry defaults (``SCAN_MODEL``,
``SCAN_ENHANCED_MODEL``). Existing rows are never touched, so after the first
boot the row is the source of truth and the env vars are initial defaults
only. A key with no row (deleted by hand, or seeding failed while the DB was
down) falls back to that same env/registry default.

**Validation.** Writes (the CLI below) are validated against the same rules
and refused when invalid. On read, an invalid provider, model, pixel cap,
global cap or allowance is ignored with a warning (log + Sentry) and the default is used, so a bad hand
edit can never widen what the registry allows: a model must be in the
allow-list of the provider it is paired with, and the registry still
re-validates the per-request spec. An invalid ``*.enabled`` row (anything but
a JSON boolean, e.g. ``"false"``, ``0`` or ``null``) fails closed instead: it
is treated as ``false`` (disabled), with the same warning. A MISSING
``*.enabled`` row still falls back to ``SCAN_ENABLED`` /
``SCAN_ENHANCED_ENABLED``, so deleting the row re-enables per the env; use
``set <key> false`` to disable.

**Cache.** Reads go through a process-local cache that is reused for
``AI_SETTINGS_CACHE_TTL_SECONDS`` (default 30). The cache is per process (per
uvicorn worker), so after a change two processes can disagree for up to the
TTL. Concurrent misses may each read the table (a few tiny rows); no lock is
used.

**Failure.** If the table cannot be read:

- and nothing was ever read in this process, :class:`AISettingsUnavailableError`
  is raised and ``/scan`` answers 503 ``ai_unavailable`` (fail closed);
- otherwise the last value read is used while it is at most
  ``AI_SETTINGS_MAX_STALE_SECONDS`` old (default 300, never below the TTL),
  and a warning is logged. Serving a recent value during a database blip is
  preferred to both failing open (ignoring a kill switch) and refusing every
  scan. Past that age an error is logged and
  :class:`AISettingsUnavailableError` is raised (fail closed). Every request
  retries the read, so the first success refreshes the cache.

**CLI.** ``python -m app.ai.settings list | get <key> | set <key> <value>
[--by NAME]``. It uses the app's ``DATABASE_URL``. ``set`` validates exactly
as above, then upserts the row with ``updated_at`` and ``updated_by`` (``--by``,
else ``$USER``, else ``cli``). Values are parsed as JSON when possible
(``true``, ``40000000``) and as a plain string otherwise (``gpt-4o``).

Nothing in a request (form field, header, query, JWT claim) can reach these
values: there is no endpoint that writes them, and readers take only a DB
session. This module never calls a provider and must not import
``app.ai.providers``.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

import sentry_sdk
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.registry import FeatureSpec, FeatureSpecError, get_feature
from app.core import database
from app.core.config import settings
from app.models.ai_settings import AISetting
from app.services.book_identifier import ScanUnavailableError

logger = logging.getLogger(__name__)

# Features whose provider, model and on/off switch come from ai_settings.
SCAN_FEATURES = ("scan.standard", "scan.enhanced")
MASTER_SWITCH = "scan.standard.enabled"

MAX_IMAGE_PIXELS_BOUNDS = (1_000_000, 100_000_000)
GLOBAL_DAILY_LIMIT_BOUNDS = (1, 1_000_000)
DAILY_ALLOWANCE_BOUNDS = (0, 1_000)
MONTHLY_ALLOWANCE_BOUNDS = (0, 100_000)
# Tiers with allowance keys: quota.<tier>.daily / quota.<tier>.monthly. Plain
# strings (the Tier enum values) so this module stays free of request-side
# imports; app/ai/quota.py maps a Tier to its keys.
QUOTA_TIERS = ("free", "premium")
MAX_MODEL_NAME_LENGTH = 100
MAX_UPDATED_BY_LENGTH = 100
SEED_ACTOR = "seed"
DEFAULT_CLI_ACTOR = "cli"
# Startup must not hang on an unreachable database.
SEED_TIMEOUT_SECONDS = 5.0


class AISettingValueError(ValueError):
    """A key is unknown or a value is outside its type, bounds or allow-list."""


class AISettingsUnavailableError(ScanUnavailableError):
    """ai_settings could not be read and nothing usable is cached (never
    read, or older than ``AI_SETTINGS_MAX_STALE_SECONDS``): fail closed.

    A ``ScanUnavailableError`` so that, if it surfaces inside ``run_feature``,
    the scan endpoint treats it like any provider outage (no provider was
    called, so the daily reservation is refunded).
    """


# ---------------------------------------------------------------------------
# Keys and validators
# ---------------------------------------------------------------------------


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise AISettingValueError("must be a JSON boolean (true or false)")
    return value


def _provider(value: Any) -> str:
    if not isinstance(value, str) or value not in ALLOWED_MODELS:
        raise AISettingValueError(
            f"must be a known provider: {', '.join(sorted(ALLOWED_MODELS))}"
        )
    return value


_ALL_MODELS = frozenset().union(*ALLOWED_MODELS.values())


def _model(value: Any) -> str:
    # The provider-specific check needs the paired provider; see _pair_problems.
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= MAX_MODEL_NAME_LENGTH
        or value not in _ALL_MODELS
    ):
        raise AISettingValueError(
            f"must be an allow-listed model: {', '.join(sorted(_ALL_MODELS))}"
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
_monthly_allowance = _bounded_int(MONTHLY_ALLOWANCE_BOUNDS)


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    validate: Callable[[Any], Any]
    # The env/registry value, read at call time: the seed, and the fallback
    # when the row is missing or invalid.
    default: Callable[[], Any]
    description: str


def _definitions() -> MappingProxyType[str, SettingDefinition]:
    defs = [
        SettingDefinition(
            MASTER_SWITCH,
            _bool,
            lambda: settings.scan_enabled,
            "all scanning on/off (seed: SCAN_ENABLED)",
        ),
        SettingDefinition(
            "scan.enhanced.enabled",
            _bool,
            lambda: settings.scan_enhanced_enabled,
            "enhanced scans on/off (seed: SCAN_ENHANCED_ENABLED)",
        ),
    ]
    for feature in SCAN_FEATURES:
        defs += [
            SettingDefinition(
                f"{feature}.provider",
                _provider,
                lambda f=feature: get_feature(f).provider,
                f"{feature} provider (seed: registry default)",
            ),
            SettingDefinition(
                f"{feature}.model",
                _model,
                lambda f=feature: get_feature(f).model,
                f"{feature} model (seed: registry default from env)",
            ),
        ]
    defs.append(
        SettingDefinition(
            "scan.max_image_pixels",
            _max_image_pixels,
            lambda: settings.scan_max_image_pixels,
            "decoded pixel cap for /scan uploads (seed: SCAN_MAX_IMAGE_PIXELS)",
        )
    )
    defs.append(
        SettingDefinition(
            "scan.global_daily_limit",
            _global_daily_limit,
            lambda: settings.scan_global_daily_limit,
            "scans per UTC day across all users (seed: SCAN_GLOBAL_DAILY_LIMIT)",
        )
    )
    for tier in QUOTA_TIERS:
        defs += [
            SettingDefinition(
                f"quota.{tier}.daily",
                _daily_allowance,
                lambda t=tier: getattr(settings, f"scan_{t}_daily_limit"),
                f"{tier} scans per user per UTC day, per scan feature "
                f"(seed: SCAN_{tier.upper()}_DAILY_LIMIT)",
            ),
            SettingDefinition(
                f"quota.{tier}.monthly",
                _monthly_allowance,
                lambda t=tier: getattr(settings, f"scan_{t}_monthly_limit"),
                f"{tier} scans per user per UTC calendar month, per scan "
                f"feature (seed: SCAN_{tier.upper()}_MONTHLY_LIMIT)",
            ),
        ]
    return MappingProxyType({d.key: d for d in defs})


DEFINITIONS: MappingProxyType[str, SettingDefinition] = _definitions()
KNOWN_KEYS = tuple(DEFINITIONS)


def _pair_problems(rows: Mapping[str, Any]) -> list[tuple[str, str]]:
    """Return ``(feature, message)`` for every provider/model pair not allowed.

    A missing provider or model is taken from the registry default.
    """
    problems = []
    for feature in SCAN_FEATURES:
        spec = get_feature(feature)
        provider = rows.get(f"{feature}.provider", spec.provider)
        model = rows.get(f"{feature}.model", spec.model)
        if model not in ALLOWED_MODELS.get(provider, frozenset()):
            # Both are known names here (validated per key), so safe to show.
            message = f"model {model!r} is not in the allow-list for {provider!r}"
            problems.append((feature, message))
    return problems


def validate_value(key: str, value: Any) -> Any:
    """Check one key's type and bounds (not the provider/model pairing)."""
    definition = DEFINITIONS.get(key)
    if definition is None:
        raise AISettingValueError(
            f"unknown key {key!r}; known keys: {', '.join(KNOWN_KEYS)}"
        )
    try:
        return definition.validate(value)
    except AISettingValueError as exc:
        raise AISettingValueError(f"{key} {exc}") from None


def validate_write(key: str, value: Any, current: Mapping[str, Any]) -> Any:
    """Validate ``key = value`` against the bounds and, for a provider or
    model, against the allow-list given the other half of the pair in
    ``current`` (already-validated rows). Returns the typed value."""
    typed = validate_value(key, value)
    for _feature, message in _pair_problems({**current, key: typed}):
        raise AISettingValueError(f"{key}: {message}")
    return typed


def _warn_ignored(key: str, reason: str) -> None:
    # The stored value is not echoed: it came from outside the code.
    logger.warning("ai_settings: ignoring %s (%s); using the default", key, reason)
    sentry_sdk.capture_message(
        f"ai_settings: ignoring invalid value for {key}", level="warning"
    )


def _is_switch(key: str) -> bool:
    return key.endswith(".enabled")


def _warn_switch_disabled(key: str, reason: str) -> None:
    # The stored value is not echoed: it came from outside the code.
    logger.warning(
        "ai_settings: invalid value for %s (%s); treated as disabled", key, reason
    )
    sentry_sdk.capture_message(
        f"ai_settings: invalid value for {key}; treated as disabled",
        level="warning",
    )


def _sanitise_rows(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate rows read from the table.

    Unknown keys are dropped. An invalid ``*.enabled`` value becomes ``False``
    (fail closed); any other invalid value is dropped, so the env/registry
    default is used.
    """
    valid: dict[str, Any] = {}
    for key, value in raw.items():
        definition = DEFINITIONS.get(key)
        if definition is None:
            logger.info("ai_settings: ignoring unknown key %r", key)
            continue
        try:
            valid[key] = definition.validate(value)
        except AISettingValueError as exc:
            if _is_switch(key):
                valid[key] = False
                _warn_switch_disabled(key, str(exc))
            else:
                _warn_ignored(key, str(exc))
    for feature, message in _pair_problems(valid):
        model_key, provider_key = f"{feature}.model", f"{feature}.provider"
        if model_key in valid:
            del valid[model_key]
            _warn_ignored(model_key, message)
        if _pair_problems(valid) and provider_key in valid:
            # Even the registry's default model is not allowed for the stored
            # provider: fall back to the registry provider too.
            del valid[provider_key]
            _warn_ignored(provider_key, message)
    return valid


# ---------------------------------------------------------------------------
# Reading (cached)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AISettings:
    """A validated view of ai_settings; missing keys use the env defaults."""

    rows: Mapping[str, Any]

    def get(self, key: str) -> Any:
        if key in self.rows:
            return self.rows[key]
        return DEFINITIONS[key].default()

    @property
    def scan_enabled(self) -> bool:
        return self.get(MASTER_SWITCH)

    @property
    def enhanced_scan_enabled(self) -> bool:
        return self.get("scan.enhanced.enabled")

    @property
    def max_image_pixels(self) -> int:
        return self.get("scan.max_image_pixels")

    @property
    def global_daily_limit(self) -> int:
        return self.get("scan.global_daily_limit")

    def allowance(self, tier: str) -> tuple[int, int]:
        """``(daily, monthly)`` scans for ``tier`` (``"free"`` or ``"premium"``)."""
        return self.get(f"quota.{tier}.daily"), self.get(f"quota.{tier}.monthly")

    def feature_enabled(self, feature: str) -> bool:
        """``scan.standard.enabled`` is the master switch for every scan."""
        if feature not in SCAN_FEATURES:
            return True
        return self.scan_enabled and self.get(f"{feature}.enabled")

    def spec_for(self, spec: FeatureSpec) -> FeatureSpec:
        """The per-request spec: ``spec`` with the provider/model overrides and
        the kill switch applied. ``replace`` re-runs the registry's own
        validation, so the registry stays the allow-list authority."""
        if spec.name not in SCAN_FEATURES:
            return spec
        enabled = spec.enabled and self.feature_enabled(spec.name)
        provider = self.rows.get(f"{spec.name}.provider", spec.provider)
        model = self.rows.get(f"{spec.name}.model", spec.model)
        try:
            return replace(spec, provider=provider, model=model, enabled=enabled)
        except FeatureSpecError:
            _warn_ignored(f"{spec.name}.model", "rejected by the registry")
            return replace(spec, enabled=enabled)


@dataclass(frozen=True)
class _CacheEntry:
    rows: Mapping[str, Any]
    loaded_at: float


_cache: _CacheEntry | None = None
# Patched in tests to move time without sleeping.
_clock: Callable[[], float] = time.monotonic


def reset_cache(rows: Mapping[str, Any] | None = None) -> None:
    """Empty the cache, or fill it with ``rows`` as if just read (tests)."""
    global _cache
    _cache = (
        None
        if rows is None
        else _CacheEntry(MappingProxyType(_sanitise_rows(rows)), _clock())
    )


async def _load_rows(db: AsyncSession) -> dict[str, Any]:
    query = select(AISetting.key, AISetting.value)
    if (
        not db.in_transaction()
        and not db.identity_map
        and not (db.new or db.dirty or db.deleted)
    ):
        # No transaction open and the session holds nothing: a short
        # transaction of its own, so no connection is left idle in a
        # transaction afterwards. Restricted to an empty session because a
        # failed outermost rollback would expire every loaded object.
        async with db.begin():
            result = await db.execute(query)
            return {key: value for key, value in result.all()}
    # Otherwise (a transaction is open, or the session holds objects or
    # pending changes): a SAVEPOINT, so a failed read rolls back only itself;
    # the session stays usable and objects it already loaded (the current
    # user) are not expired. When no transaction was open, the session's
    # autobegun transaction stays open until the caller commits or closes.
    async with db.begin_nested():
        result = await db.execute(query)
        return {key: value for key, value in result.all()}


async def get_ai_settings(db: AsyncSession) -> AISettings:
    """Return the current settings, from the cache when it is fresh.

    Raises :class:`AISettingsUnavailableError` when the table cannot be read
    and nothing has been read before in this process, or the last value read
    is older than ``AI_SETTINGS_MAX_STALE_SECONDS``.
    """
    global _cache
    now = _clock()
    entry = _cache
    if entry is not None and now - entry.loaded_at < (
        settings.ai_settings_cache_ttl_seconds
    ):
        return AISettings(entry.rows)
    try:
        raw = await _load_rows(db)
    except Exception as exc:  # noqa: BLE001 - any read failure is handled alike
        if entry is not None:
            # Measured after the failed read, so a slow failure cannot stretch
            # the stale window past the limit.
            age = _clock() - entry.loaded_at
            if age <= settings.ai_settings_max_stale_seconds:
                logger.warning(
                    "ai_settings: read failed (%s); using the value read %.0fs ago",
                    type(exc).__name__,
                    age,
                )
                return AISettings(entry.rows)
            logger.error(
                "ai_settings: read failed (%s) and the cached value is %.0fs old "
                "(max %ds); failing closed",
                type(exc).__name__,
                age,
                settings.ai_settings_max_stale_seconds,
            )
            raise AISettingsUnavailableError("ai_settings unavailable") from None
        logger.error(
            "ai_settings: read failed (%s) and nothing is cached; failing closed",
            type(exc).__name__,
        )
        raise AISettingsUnavailableError("ai_settings unavailable") from None
    rows = MappingProxyType(_sanitise_rows(raw))
    _cache = _CacheEntry(rows, now)
    return AISettings(rows)


# ---------------------------------------------------------------------------
# Seeding and writing
# ---------------------------------------------------------------------------


def seed_values() -> dict[str, Any]:
    """Every key's env/registry default, validated like a write."""
    values: dict[str, Any] = {}
    for key, definition in DEFINITIONS.items():
        values[key] = validate_value(key, definition.default())
    for _feature, message in _pair_problems(values):
        raise AISettingValueError(message)
    return values


async def seed_ai_settings(db: AsyncSession) -> int:
    """Insert every missing key; existing rows are left as they are.

    Returns how many rows were inserted. Idempotent.
    """
    stmt = (
        insert(AISetting)
        .values(
            [
                {"key": key, "value": value, "updated_by": SEED_ACTOR}
                for key, value in seed_values().items()
            ]
        )
        .on_conflict_do_nothing(index_elements=[AISetting.key])
        .returning(AISetting.key)
    )
    result = await db.execute(stmt)
    inserted = len(result.scalars().all())
    await db.commit()
    return inserted


def _open_session() -> AsyncSession:
    return database.AsyncSessionLocal()


async def _dispose_engine() -> None:
    await database.engine.dispose()


async def seed_ai_settings_at_startup() -> None:
    """Seed at app startup. Never raises: with the DB unreachable the app
    still starts, and requests fail closed until the table can be read."""
    try:
        async with _open_session() as db:
            inserted = await asyncio.wait_for(
                seed_ai_settings(db), timeout=SEED_TIMEOUT_SECONDS
            )
        logger.info("ai_settings: seeded %d missing key(s)", inserted)
    except Exception as exc:  # noqa: BLE001 - startup must not crash here
        logger.warning(
            "ai_settings: seeding skipped (%s); scans fail closed until the "
            "table can be read",
            type(exc).__name__,
        )


async def read_rows(db: AsyncSession) -> dict[str, AISetting]:
    """Every stored row, uncached (CLI)."""
    result = await db.execute(select(AISetting))
    return {row.key: row for row in result.scalars().all()}


def _validate_actor(updated_by: str) -> str:
    updated_by = updated_by.strip()
    if not 0 < len(updated_by) <= MAX_UPDATED_BY_LENGTH or not updated_by.isprintable():
        raise AISettingValueError(
            f"--by must be 1..{MAX_UPDATED_BY_LENGTH} printable characters"
        )
    return updated_by


async def set_setting(
    db: AsyncSession, key: str, value: Any, *, updated_by: str
) -> Any:
    """Validate and upsert one key. Returns the stored (typed) value.

    Takes effect in each process within the cache TTL.
    """
    updated_by = _validate_actor(updated_by)
    validate_value(key, value)
    stored = await read_rows(db)
    current = _sanitise_rows({k: row.value for k, row in stored.items()})
    typed = validate_write(key, value, current)
    stmt = insert(AISetting).values(key=key, value=typed, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AISetting.key],
        set_={
            "value": stmt.excluded.value,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.commit()
    return typed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_cli_value(text: str) -> Any:
    """JSON when it parses (``true``, ``40000000``), else the plain string."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _printable(text: str) -> str:
    """``text`` with every non-printable character (escape sequences,
    newlines) replaced by ``?``, so a hand-edited row cannot drive the
    operator's terminal."""
    return "".join(c if c.isprintable() else "?" for c in text)


def _describe(key: str, stored: Mapping[str, AISetting]) -> str:
    row = stored.get(key)
    if row is None:
        value = DEFINITIONS[key].default()
        return f"{key} = {json.dumps(value)}  (no row; env/registry default)"
    current = _sanitise_rows({k: r.value for k, r in stored.items()})
    if key not in current:
        value = DEFINITIONS[key].default()
        return (
            f"{key} = {json.dumps(value)}  (stored value is INVALID and ignored; "
            "env/registry default in use)"
        )
    # json.dumps escapes control characters, so values print safely.
    try:
        DEFINITIONS[key].validate(row.value)
    except AISettingValueError:
        return f"{key} = false  (stored value is INVALID; treated as disabled)"
    updated_by = _printable(row.updated_by or "?")
    return (
        f"{key} = {json.dumps(row.value)}  "
        f"(updated {row.updated_at:%Y-%m-%d %H:%M:%S %Z} by {updated_by})"
    )


async def _run(args: argparse.Namespace) -> int:
    async with _open_session() as db:
        if args.command == "set":
            updated_by = args.by or os.environ.get("USER") or DEFAULT_CLI_ACTOR
            typed = await set_setting(
                db, args.key, parse_cli_value(args.value), updated_by=updated_by
            )
            print(
                f"{args.key} = {json.dumps(typed)}  (takes effect within "
                f"{settings.ai_settings_cache_ttl_seconds}s in every process)"
            )
            return 0
        stored = await read_rows(db)
        keys = KNOWN_KEYS if args.command == "list" else (args.key,)
        for key in keys:
            print(_describe(key, stored))
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.ai.settings",
        description="Read or change the ai_settings dials and kill switches.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    set_cmd = sub.add_parser("set", help="validate and store one value")
    set_cmd.add_argument("key", choices=KNOWN_KEYS)
    set_cmd.add_argument("value", help="JSON (true, 40000000) or a plain string")
    set_cmd.add_argument("--by", help="who made the change (default: $USER)")
    get_cmd = sub.add_parser("get", help="show one key")
    get_cmd.add_argument("key", choices=KNOWN_KEYS)
    sub.add_parser("list", help="show every key")
    args = parser.parse_args(argv)

    if args.command == "set":
        # Fail fast on a bad value before touching the database.
        try:
            validate_value(args.key, parse_cli_value(args.value))
        except AISettingValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    async def _run_and_dispose() -> int:
        try:
            return await _run(args)
        finally:
            await _dispose_engine()

    try:
        return asyncio.run(_run_and_dispose())
    except AISettingValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (SQLAlchemyError, OSError) as exc:
        print(
            f"error: could not use the database ({type(exc).__name__})",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
