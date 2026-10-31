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
``quota.free.found_daily``  int      0..1,000 scans per user per UTC day in
                                     which the provider found books (#577)
``quota.free.``             int      0..1,000 scans per user per UTC day in
``no_match_daily``                   which the provider answered properly
                                     but found no book (#577)
``quota.premium.``          int      0..1,000 (as ``quota.free.found_daily``,
``found_daily``                      Premium tier)
``quota.premium.``          int      0..1,000 (as ``quota.free.no_match_daily``,
``no_match_daily``                   Premium tier)
``breaker.free_daily_``     int      0..1,000,000,000 micro-dollars of AI spend
``ceiling_micros``                   per UTC day, all tiers counted, above which
                                     Free-tier scans get 503 ``ai_budget_reached``
                                     (Premium continues; ``app/ai/breaker.py``)
``scan.enhanced.``          int      0..20 zero-result enhanced scans
``zero_result_threshold``            recorded per user per window before the
                                     action applies (#533)
``scan.enhanced.``          int      1..720 hours: the sliding window the
``zero_result_window_hours``         zero-result count covers
``scan.enhanced.``          str      ``charge`` (record and warn only; every
``zero_result_action``               scan uses the shared daily unit) or
                                     ``throttle`` (enhanced scans refused, 429
                                     ``enhanced_throttled``, until the window
                                     clears)
``scan_records.enabled``    bool     false switches off scan records
                                     (#585): no lookup, no save; every scan
                                     takes the normal (charged) path
``scan_records.``           int      1..30 days a scan record is kept and
``retention_days``                   served; older ones are purged daily
``scan_records.``           int      1..10,000 scan records per user; the
``max_per_user``                     oldest are evicted first
``scan.requests_per_``      int      1..10,000 ``/scan`` requests per user per
``user_daily``                       UTC day, new photo or repeat, refused or
                                     not (#586; 429
                                     ``scan_requests_daily_limit``)
==========================  =======  =========================================

Enhanced scans are Premium-only (#533): non-Premium tiers are refused (403
``enhanced_requires_premium``) before any reservation. The three ``scan.enhanced.zero_result_*`` keys are seeded from
code defaults (3, 24, ``charge``), not from the env, as are the three
``scan_records.*`` keys (``enabled`` ``true``, ``retention_days`` 30,
``max_per_user`` 500), and ``scan.requests_per_user_daily`` (100, #586).

The ``quota.*`` allowances are per tier. The found allowance is shared by
standard and enhanced scans (one ``ai_quota`` counter per user and UTC day,
under ``quota.SCAN_QUOTA_FEATURE``, OD-1); the no-match allowance has its own
counter (``quota.SCAN_NO_MATCH_QUOTA_FEATURE``) and only standard scans move
to it (#577); see ``app/ai/quota.py``.

**Found / no-match (#577).** ``quota.<tier>.daily`` is retired
(:data:`RETIRED_KEYS`): it became ``quota.<tier>.found_daily``. Carry-over:
an operator may have customised the old key, so a stored ``quota.<tier>.daily``
row that was NOT written by the seed (``updated_by`` is not ``seed``) and is
valid is used as ``quota.<tier>.found_daily`` while no ``found_daily`` row
exists: at seeding (the new row is inserted with the old value) and on read
(when seeding has not run yet). A seeded old row (the env default) is not
carried, so the new default applies (Premium: 30 before, 5 now). Once a
``found_daily`` row exists the old row is ignored. See :func:`carried_over`.

**No monthly keys (#582).** ``quota.free.monthly``, ``quota.premium.monthly``
and ``quota.premium.enhanced_monthly`` are retired (:data:`RETIRED_KEYS`):
there is no monthly limit. The CLI rejects them as unknown keys, seeding no
longer writes them, and a row still stored for one is ignored on read
(without a warning). Migration 0018 (#594) deleted the stored rows.

**Seeding.** At startup :func:`seed_ai_settings_at_startup` inserts every key
that is missing, from the env (``SCAN_ENABLED``, ``SCAN_ENHANCED_ENABLED``,
``SCAN_MAX_IMAGE_PIXELS``, ``SCAN_GLOBAL_DAILY_LIMIT``, ``SCAN_FREE_DAILY_LIMIT``,
``SCAN_PREMIUM_DAILY_LIMIT``, which seed the ``found_daily`` keys since #577,
``SCAN_FREE_NO_MATCH_DAILY_LIMIT``, ``SCAN_PREMIUM_NO_MATCH_DAILY_LIMIT``,
``SCAN_FREE_DAILY_CEILING_MICROS``), the code
defaults above and the registry defaults (``SCAN_MODEL``,
``SCAN_ENHANCED_MODEL``). Existing rows are never touched, so after the first
boot the row is the source of truth and the env vars are initial defaults
only. A key with no row (deleted by hand, or seeding failed while the DB was
down) falls back to that same env/registry default.

**Validation.** Writes (the CLI below) are validated against the same rules
and refused when invalid. On read, an invalid provider, model, pixel cap,
global cap, allowance, cost ceiling, zero-result, scan-record or request-limit
setting is ignored with a warning (log + Sentry) and the default is used, so a bad hand
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

**CLI.** ``python -m app.ai.settings list`` groups the keys by area
(:data:`AREA_RULES`) and shows each key's value, default and bounds; every key
also has a row in ``docs/scan-limits.md`` (a unit test fails when one is
missing). ``python -m app.ai.settings list | get <key> | set <key> <value>
[--by NAME]``. It uses the app's ``DATABASE_URL``. ``set`` validates exactly
as above, then upserts the row with ``updated_at`` and ``updated_by`` (``--by``,
else ``$USER``, else ``cli``). Values are parsed as JSON when possible
(``true``, ``40000000``) and as a plain string otherwise (``gpt-4o``).

**Switching provider.** A provider and its model are a pair, so ``set`` of a
provider alone is refused while the stored model belongs to the other
provider. ``python -m app.ai.settings switch <feature> <provider> [--model M]
[--by NAME]`` writes both rows in one transaction; without ``--model`` the
model is the registry's per-provider default (``registry.PROVIDER_MODELS``:
Haiku 5.5 for ``scan.standard``, Sonnet 5.5 for ``scan.enhanced`` on
``anthropic``). Switching back to ``openai`` the same way restores the OpenAI
model.

Nothing in a request (form field, header, query, JWT claim) can reach these
values: there is no endpoint that writes them, and readers take only a DB
session. This package never calls a provider and no module in it may import
``app.ai.providers``.

**Package layout (#807).** One module per job, all re-exported here:
``constants`` (keys, bounds, code defaults), ``errors``, ``validators``
(per-key checks), ``definitions`` (the registry, as data), ``areas`` (the
``list`` groups), ``validation`` (writes and read sanitising), ``view``
(:class:`AISettings`), ``cache`` (the cached read), ``store`` (seed, read,
write) and ``cli`` (with ``__main__``, so ``python -m app.ai.settings``
works as before).

**Calls go through the package.** Callers and tests use ``from app.ai import
settings as ai_settings`` and ``ai_settings.<name>``, and tests patch
``ai_settings.<name>`` (the clock, the session factory, ``read_rows``,
defaults, ``sentry_sdk``...). So every module calls the package's functions,
reads its tunables and module objects (``settings``, ``database``,
``sentry_sdk``, ``logger``) and keeps the cache state (``_cache``,
``_clock``) through the package at call time: a patch on the package reaches
the code that runs, wherever it lives, and the cache exists once. Classes
and exceptions are imported directly: ``isinstance``/``except`` see the same
class either way. ``ai_settings.settings`` is the app's
``app.core.config.settings``, as it was on the single module.
"""

import logging
import time
from collections.abc import Callable

import sentry_sdk

from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.registry import (
    FeatureSpec,
    FeatureSpecError,
    default_model,
    get_feature,
)
from app.ai.settings.areas import AREA_RULES, OTHER_AREA, area_of, keys_by_area
from app.ai.settings.cache import (
    _CacheEntry,
    _load_rows,
    _with_carry_over,
    get_ai_settings,
    reset_cache,
)
from app.ai.settings.cli import (
    _default_and_bounds,
    _describe,
    _printable,
    _run,
    main,
    parse_cli_value,
)
from app.ai.settings.constants import (
    _ALL_MODELS,
    DAILY_ALLOWANCE_BOUNDS,
    DEFAULT_CLI_ACTOR,
    DEFAULT_SCAN_RECORDS_ENABLED,
    DEFAULT_SCAN_RECORDS_MAX_PER_USER,
    DEFAULT_SCAN_RECORDS_RETENTION_DAYS,
    DEFAULT_SCAN_REQUESTS_PER_USER_DAILY,
    DEFAULT_ZERO_RESULT_ACTION,
    DEFAULT_ZERO_RESULT_THRESHOLD,
    DEFAULT_ZERO_RESULT_WINDOW_HOURS,
    FREE_DAILY_CEILING_KEY,
    FREE_DAILY_CEILING_MICROS_BOUNDS,
    GLOBAL_DAILY_LIMIT_BOUNDS,
    MASTER_SWITCH,
    MAX_IMAGE_PIXELS_BOUNDS,
    MAX_MODEL_NAME_LENGTH,
    MAX_UPDATED_BY_LENGTH,
    QUOTA_TIERS,
    RETIRED_KEYS,
    SCAN_FEATURES,
    SCAN_RECORDS_ENABLED_KEY,
    SCAN_RECORDS_MAX_PER_USER_BOUNDS,
    SCAN_RECORDS_MAX_PER_USER_KEY,
    SCAN_RECORDS_RETENTION_DAYS_BOUNDS,
    SCAN_RECORDS_RETENTION_DAYS_KEY,
    SCAN_REQUESTS_PER_USER_DAILY_BOUNDS,
    SCAN_REQUESTS_PER_USER_DAILY_KEY,
    SEED_ACTOR,
    SEED_TIMEOUT_SECONDS,
    ZERO_RESULT_ACTION_CHARGE,
    ZERO_RESULT_ACTION_KEY,
    ZERO_RESULT_ACTION_THROTTLE,
    ZERO_RESULT_ACTIONS,
    ZERO_RESULT_THRESHOLD_BOUNDS,
    ZERO_RESULT_THRESHOLD_KEY,
    ZERO_RESULT_WINDOW_HOURS_BOUNDS,
    ZERO_RESULT_WINDOW_HOURS_KEY,
)
from app.ai.settings.definitions import (
    DEFINITIONS,
    KNOWN_KEYS,
    SettingDefinition,
    _definitions,
)
from app.ai.settings.errors import AISettingsUnavailableError, AISettingValueError
from app.ai.settings.store import (
    _dispose_engine,
    _open_session,
    _upsert,
    _validate_actor,
    read_rows,
    seed_ai_settings,
    seed_ai_settings_at_startup,
    seed_values,
    set_setting,
    switch_provider,
    validate_switch,
)
from app.ai.settings.validation import (
    _is_switch,
    _pair_problems,
    _sanitise_rows,
    _warn_ignored,
    _warn_switch_disabled,
    carried_over,
    validate_value,
    validate_write,
)
from app.ai.settings.validators import (
    _BOOL_BOUNDS,
    _bool,
    _bounded_int,
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
    legacy_daily_key,
    no_match_key,
)
from app.ai.settings.view import AISettings
from app.core import database
from app.core.config import settings
from app.models.ai_settings import AISetting
from app.services.book_identifier import ScanUnavailableError

# The one logger of the package, under the single module's name.
logger = logging.getLogger(__name__)

# The process-local cache (``cache.py`` reads and writes it here, so there is
# one per process) and its clock, patched in tests to move time without
# sleeping.
_cache: _CacheEntry | None = None
_clock: Callable[[], float] = time.monotonic

# Every name app/ai/settings.py had as one module (#807; stdlib, typing and
# SQLAlchemy imports aside). tests/unit/test_ai_settings_package.py guards
# this list.
__all__ = [
    "ALLOWED_MODELS",
    "AREA_RULES",
    "DAILY_ALLOWANCE_BOUNDS",
    "DEFAULT_CLI_ACTOR",
    "DEFAULT_SCAN_RECORDS_ENABLED",
    "DEFAULT_SCAN_RECORDS_MAX_PER_USER",
    "DEFAULT_SCAN_RECORDS_RETENTION_DAYS",
    "DEFAULT_SCAN_REQUESTS_PER_USER_DAILY",
    "DEFAULT_ZERO_RESULT_ACTION",
    "DEFAULT_ZERO_RESULT_THRESHOLD",
    "DEFAULT_ZERO_RESULT_WINDOW_HOURS",
    "DEFINITIONS",
    "FREE_DAILY_CEILING_KEY",
    "FREE_DAILY_CEILING_MICROS_BOUNDS",
    "GLOBAL_DAILY_LIMIT_BOUNDS",
    "KNOWN_KEYS",
    "MASTER_SWITCH",
    "MAX_IMAGE_PIXELS_BOUNDS",
    "MAX_MODEL_NAME_LENGTH",
    "MAX_UPDATED_BY_LENGTH",
    "OTHER_AREA",
    "QUOTA_TIERS",
    "RETIRED_KEYS",
    "SCAN_FEATURES",
    "SCAN_RECORDS_ENABLED_KEY",
    "SCAN_RECORDS_MAX_PER_USER_BOUNDS",
    "SCAN_RECORDS_MAX_PER_USER_KEY",
    "SCAN_RECORDS_RETENTION_DAYS_BOUNDS",
    "SCAN_RECORDS_RETENTION_DAYS_KEY",
    "SCAN_REQUESTS_PER_USER_DAILY_BOUNDS",
    "SCAN_REQUESTS_PER_USER_DAILY_KEY",
    "SEED_ACTOR",
    "SEED_TIMEOUT_SECONDS",
    "ZERO_RESULT_ACTIONS",
    "ZERO_RESULT_ACTION_CHARGE",
    "ZERO_RESULT_ACTION_KEY",
    "ZERO_RESULT_ACTION_THROTTLE",
    "ZERO_RESULT_THRESHOLD_BOUNDS",
    "ZERO_RESULT_THRESHOLD_KEY",
    "ZERO_RESULT_WINDOW_HOURS_BOUNDS",
    "ZERO_RESULT_WINDOW_HOURS_KEY",
    "_ALL_MODELS",
    "_BOOL_BOUNDS",
    "AISetting",
    "AISettingValueError",
    "AISettings",
    "AISettingsUnavailableError",
    "FeatureSpec",
    "FeatureSpecError",
    "ScanUnavailableError",
    "SettingDefinition",
    "_CacheEntry",
    "_bool",
    "_bounded_int",
    "_cache",
    "_clock",
    "_daily_allowance",
    "_default_and_bounds",
    "_definitions",
    "_describe",
    "_dispose_engine",
    "_free_daily_ceiling",
    "_global_daily_limit",
    "_is_switch",
    "_load_rows",
    "_max_image_pixels",
    "_model",
    "_open_session",
    "_pair_problems",
    "_printable",
    "_provider",
    "_range",
    "_run",
    "_sanitise_rows",
    "_scan_records_max_per_user",
    "_scan_records_retention_days",
    "_scan_requests_per_user_daily",
    "_upsert",
    "_validate_actor",
    "_warn_ignored",
    "_warn_switch_disabled",
    "_with_carry_over",
    "_zero_result_action",
    "_zero_result_threshold",
    "_zero_result_window_hours",
    "area_of",
    "carried_over",
    "database",
    "default_model",
    "found_key",
    "get_ai_settings",
    "get_feature",
    "keys_by_area",
    "legacy_daily_key",
    "logger",
    "main",
    "no_match_key",
    "parse_cli_value",
    "read_rows",
    "reset_cache",
    "seed_ai_settings",
    "seed_ai_settings_at_startup",
    "seed_values",
    "sentry_sdk",
    "set_setting",
    "settings",
    "switch_provider",
    "validate_switch",
    "validate_value",
    "validate_write",
]
