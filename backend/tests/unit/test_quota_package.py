"""The ``app.ai.quota`` package facade (#806).

quota.py was split into a package. Callers and tests keep using
``from app.ai import quota`` and ``quota.<name>``, and many tests patch
``quota.<name>``; these tests guard both."""

import ast
import importlib
import logging
import pkgutil
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from app.ai import quota
from app.ai.quota import types as quota_types

PACKAGE_DIR = Path(quota.__file__).resolve().parent

# Every top-level name app/ai/quota.py had before the split (stdlib and
# SQLAlchemy imports aside). Dropping one breaks a caller or a patch.
FACADE_NAMES = [
    "AIEnhancedZeroResult",
    "AIInflight",
    "AIQuota",
    "AISettings",
    "Allowance",
    "CLEANUP_TIMEOUT_SECONDS",
    "CostCeilingReached",
    "DAILY_RETENTION_DAYS",
    "DailyLimitReached",
    "ENHANCED_FEATURE_TAG",
    "EnhancedRequiresPremium",
    "EnhancedThrottled",
    "FLAG_DAILY",
    "GIVE_BACK_DONE",
    "GIVE_BACK_FAILED",
    "GIVE_BACK_TIMED_OUT",
    "GlobalCapacityReached",
    "IN_PROGRESS_RETRY_AFTER_SECONDS",
    "LEASE_GRACE_SECONDS",
    "MONTHLY_RETENTION_MONTHS",
    "NON_AI_QUOTA_FEATURES",
    "NO_MATCH_COUNTED",
    "NO_MATCH_FREE",
    "NoMatch",
    "NoMatchMoved",
    "PURGE_BATCH_SIZE",
    "PURGE_MAX_BATCHES",
    "QuotaRefusal",
    "Reservation",
    "SCAN_NO_MATCH_QUOTA_FEATURE",
    "SCAN_QUOTA_FEATURE",
    "SCAN_REQUESTS_QUOTA_FEATURE",
    "ScanInProgress",
    "ScanRequestsDailyLimitReached",
    "Tier",
    "UNIT_FLAGS",
    "UNIT_GIVEN_BACK",
    "UNIT_KEPT_FALLBACK_FAILED",
    "UNIT_KEPT_GIVE_BACK_FAILED",
    "UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED",
    "UNIT_LEASE_LOST",
    "UNIT_NOT_TAKEN",
    "Usage",
    "User",
    "WINDOW_DAY",
    "WINDOW_MONTH",
    "ZERO_RESULT_ACTION_THROTTLE",
    "ZERO_RESULT_RETENTION_DAYS",
    "ZeroResult",
    "ZeroResultRecorded",
    "_UNIT_TAGS",
    "_Unit",
    "_alert",
    "_clock_timestamp",
    "_daily_unit",
    "_flag_column",
    "_give_back",
    "_held",
    "_in_window",
    "_purge_kind",
    "_return_unit",
    "_tier_tag",
    "_unit_upsert",
    "acquire_lease",
    "alerts",
    "allowance_for",
    "breaker",
    "count_scan_request",
    "enhanced_remaining",
    "enhanced_remaining_for",
    "entitlements",
    "give_back_unit",
    "global_units_filter",
    "global_units_today",
    "hold_lease_lock",
    "lease_seconds",
    "logger",
    "months_before",
    "move_to_no_match",
    "purge_cutoffs",
    "purge_old_quota",
    "purge_old_zero_results",
    "record_zero_result",
    "refund",
    "refund_unit",
    "release",
    "release_lease",
    "renew_lease",
    "require_enhanced_entitlement",
    "reserve",
    "reserve_unit",
    "retry_after_seconds",
    "seconds_until_utc_midnight",
    "send_no_match_ceiling_warning",
    "send_unit_kept_warning",
    "send_zero_result_warning",
    "take_unit",
    "units_used",
    "usage_for",
    "utc_now",
    "utc_today",
    "warned_in_window",
    "zero_result_charged",
    "zero_result_count",
    "zero_result_throttle",
]

# Numbers tests tune by patching the package; read through it at call time.
TUNABLES = {
    "CLEANUP_TIMEOUT_SECONDS",
    "DAILY_RETENTION_DAYS",
    "LEASE_GRACE_SECONDS",
    "MONTHLY_RETENTION_MONTHS",
    "PURGE_BATCH_SIZE",
    "PURGE_MAX_BATCHES",
    "ZERO_RESULT_RETENTION_DAYS",
}


def _submodules():
    return sorted(p for p in PACKAGE_DIR.glob("*.py") if p.name != "__init__.py")


def _package_functions() -> set[str]:
    names = set()
    for path in _submodules():
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


def test_the_package_exposes_every_name_quota_py_had():
    missing = [name for name in FACADE_NAMES if not hasattr(quota, name)]
    assert missing == []
    assert sorted(quota.__all__) == sorted(FACADE_NAMES)


def test_every_package_function_is_re_exported():
    assert _package_functions() - set(quota.__all__) == set()


def test_no_module_calls_a_quota_function_or_tunable_by_bare_name():
    """A bare-name call would skip a patch on ``quota.<name>``: every call
    goes through the package (``quota.<name>``)."""
    functions = _package_functions()
    direct = []
    for path in _submodules():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and (node.id in functions or node.id in TUNABLES)
            ):
                direct.append(f"{path.name}:{node.lineno} {node.id}")
    assert direct == []


def test_the_modules_log_under_the_old_logger_name():
    assert quota.logger is logging.getLogger("app.ai.quota")
    checked = []
    for info in pkgutil.iter_modules(quota.__path__):
        mod = importlib.import_module(f"{quota.__name__}.{info.name}")
        logger = getattr(mod, "logger", None)
        if logger is not None:
            checked.append(info.name)
            assert logger.name == "app.ai.quota", info.name
    assert checked, "no submodule defines a module-level logger"


@pytest.mark.parametrize(
    "name",
    [
        "GlobalCapacityReached",
        "CostCeilingReached",
        "ScanInProgress",
        "DailyLimitReached",
        "ScanRequestsDailyLimitReached",
        "EnhancedRequiresPremium",
        "EnhancedThrottled",
    ],
)
def test_refusals_are_one_class_each_under_quota_refusal(name):
    cls = getattr(quota, name)
    assert cls is getattr(quota_types, name)
    assert issubclass(cls, quota.QuotaRefusal)


def test_a_patch_on_the_package_reaches_a_sibling_module():
    noon = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    with patch.object(quota, "utc_now", lambda: noon):
        assert quota.utc_today() == date(2026, 3, 1)
        assert quota.seconds_until_utc_midnight() == 12 * 3600
    with patch.object(quota, "months_before", lambda day, months: date(1999, 1, 1)):
        assert quota.purge_cutoffs(date(2026, 3, 1))[1] == date(1999, 1, 1)
    with patch.object(quota, "LEASE_GRACE_SECONDS", 1):
        assert quota.lease_seconds(10) == 11
