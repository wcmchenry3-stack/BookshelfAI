"""Keys, bounds and code defaults of ``app.ai.settings``.

Code reads these through the package (``ai_settings.<NAME>``) at call time,
so a patch on the package reaches it; see the package docstring.
"""

from app.ai.models_allowlist import ALLOWED_MODELS

# Features whose provider, model and on/off switch come from ai_settings.
SCAN_FEATURES = ("scan.standard", "scan.enhanced")
MASTER_SWITCH = "scan.standard.enabled"

MAX_IMAGE_PIXELS_BOUNDS = (1_000_000, 100_000_000)
GLOBAL_DAILY_LIMIT_BOUNDS = (1, 1_000_000)
DAILY_ALLOWANCE_BOUNDS = (0, 1_000)
# Micro-dollars per UTC day (1,000,000 = $1); 0 pauses every Free scan.
FREE_DAILY_CEILING_MICROS_BOUNDS = (0, 1_000_000_000)
FREE_DAILY_CEILING_KEY = "breaker.free_daily_ceiling_micros"
# Enhanced scans (#533): the zero-result abuse limits.
ZERO_RESULT_THRESHOLD_KEY = "scan.enhanced.zero_result_threshold"
ZERO_RESULT_WINDOW_HOURS_KEY = "scan.enhanced.zero_result_window_hours"
ZERO_RESULT_ACTION_KEY = "scan.enhanced.zero_result_action"
ZERO_RESULT_THRESHOLD_BOUNDS = (0, 20)
ZERO_RESULT_WINDOW_HOURS_BOUNDS = (1, 720)
ZERO_RESULT_ACTION_CHARGE = "charge"
ZERO_RESULT_ACTION_THROTTLE = "throttle"
ZERO_RESULT_ACTIONS = (ZERO_RESULT_ACTION_CHARGE, ZERO_RESULT_ACTION_THROTTLE)
DEFAULT_ZERO_RESULT_THRESHOLD = 3
DEFAULT_ZERO_RESULT_WINDOW_HOURS = 24
DEFAULT_ZERO_RESULT_ACTION = ZERO_RESULT_ACTION_CHARGE
# Scan records (#585): per user, keyed by the normalised-image SHA-256.
SCAN_RECORDS_ENABLED_KEY = "scan_records.enabled"
SCAN_RECORDS_RETENTION_DAYS_KEY = "scan_records.retention_days"
SCAN_RECORDS_MAX_PER_USER_KEY = "scan_records.max_per_user"
# At most 30 days (decision #573), to be revisited with the privacy review.
SCAN_RECORDS_RETENTION_DAYS_BOUNDS = (1, 30)
SCAN_RECORDS_MAX_PER_USER_BOUNDS = (1, 10_000)
DEFAULT_SCAN_RECORDS_ENABLED = True
DEFAULT_SCAN_RECORDS_RETENTION_DAYS = 30
DEFAULT_SCAN_RECORDS_MAX_PER_USER = 500
# /scan requests per user per UTC day (#586, decision #573 point 3).
SCAN_REQUESTS_PER_USER_DAILY_KEY = "scan.requests_per_user_daily"
SCAN_REQUESTS_PER_USER_DAILY_BOUNDS = (1, 10_000)
DEFAULT_SCAN_REQUESTS_PER_USER_DAILY = 100
# Keys removed by #582 (no monthly limits). Unknown to the CLI and the seed;
# migration 0018 (#594) deleted the stored rows. Kept so a row restored from a
# backup or written by an older instance is skipped on read without a warning.
# #577 retired quota.<tier>.daily: it became quota.<tier>.found_daily (with
# the carry-over in carried_over()).
RETIRED_KEYS = frozenset(
    {
        "quota.free.monthly",
        "quota.premium.monthly",
        "quota.premium.enhanced_monthly",
        "quota.free.daily",
        "quota.premium.daily",
    }
)
# Tiers with allowance keys: quota.<tier>.found_daily and
# quota.<tier>.no_match_daily (#577). Plain
# strings (the Tier enum values) so this module stays free of request-side
# imports; app/ai/quota.py maps a Tier to its keys.
QUOTA_TIERS = ("free", "premium")
MAX_MODEL_NAME_LENGTH = 100
MAX_UPDATED_BY_LENGTH = 100
SEED_ACTOR = "seed"
DEFAULT_CLI_ACTOR = "cli"
# Startup must not hang on an unreachable database.
SEED_TIMEOUT_SECONDS = 5.0

_ALL_MODELS = frozenset().union(*ALLOWED_MODELS.values())
