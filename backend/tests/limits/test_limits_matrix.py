"""The scan limits matrix (#584): one row per scan outcome and tier.

Each row says, for a tier (Free / Premium) and a scan (standard / enhanced),
given the starting counters and any ``ai_settings`` overrides, what the
provider does, and then what must happen:

- the HTTP status and ``detail`` code the client gets (200 = allowed);
- whether the provider was reached;
- how each counter moves: the shared daily and monthly units, the Premium
  enhanced monthly unit, the service-wide day count (``global``) and the
  user's uncharged zero-result rows;
- what the day's AI spend settles: nothing, the estimate, or the priced cost.

Every row runs the real code path on real Postgres: ``/scan`` (below the rate
limiter) -> ``quota.reserve`` -> the gateway -> ``quota.release`` /
``refund``, with the provider faked at the gateway boundary. After every row
no lease is left behind, nothing stays reserved in the spend, and no counter
is negative.

ADDING ROWS (read before adding or changing a scan limit)
--------------------------------------------------------
Any story that adds or changes a scan limit adds its rows to ``CASES`` below
(see ``docs/claude/testing.md``, "Scan limits matrix"). Rules that do not
exist yet get their rows from their own stories:

- #573 the 100/day request limit
- #577 found / no-match counters
- #582 removing the monthly window (the monthly rows here change then)
- #583 Premium escalation
- #574 retries
- #572 malformed replies

Write a row as ``Case(...)`` with a readable ``id``
(``<tier>-<scan>-<what happens>``). Starting counters may be ``LIMIT`` (at
the tier's limit for that window). If a new rule needs a new input or
expectation, add a field to ``Start`` / ``Case`` with a default that keeps
the existing rows unchanged.
"""

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.ai import quota
from app.ai.entitlements import Tier
from tests.limits import _harness as h

pytestmark = pytest.mark.integration

FREE, PREMIUM = Tier.FREE, Tier.PREMIUM

# Starting counter "at the limit" for that window and tier.
LIMIT = "limit"

# The settings every row starts from (rows override keys). The numbers are
# today's defaults (app/core/config.py, app/ai/settings.py), written out so
# the table reads on its own.
BASE: dict[str, Any] = {
    "scan.standard.enabled": True,
    "scan.enhanced.enabled": True,
    "scan.global_daily_limit": 2_000,
    "quota.free.daily": 5,
    "quota.free.monthly": 5,
    "quota.premium.daily": 30,
    "quota.premium.monthly": 300,
    "quota.premium.enhanced_monthly": 20,
    "breaker.free_daily_ceiling_micros": 10_000_000,
    "scan.enhanced.zero_result_threshold": 3,
    "scan.enhanced.zero_result_window_hours": 24,
    "scan.enhanced.zero_result_action": "charge",
}

ALLOWED = 200
# What a scan costs the day's spend after the request.
NO_SPEND, ESTIMATE, COST = "none", "estimate", "cost"


@dataclass(frozen=True)
class Start:
    daily: int | str = 0
    monthly: int | str = 0
    enhanced: int | str = 0
    # Day units of OTHER users today (the service-wide count), or LIMIT.
    global_others: int | str = 0
    # Settled spend today in micro-dollars, or LIMIT (the Free ceiling minus
    # less than one estimate: the next Free scan does not fit).
    spend: int | str = 0
    zero_results: int = 0
    lease_held: bool = False


@dataclass(frozen=True)
class Delta:
    daily: int = 0
    monthly: int = 0
    enhanced: int = 0
    zero_results: int = 0


CHARGED = Delta(daily=1, monthly=1)
CHARGED_ENHANCED = Delta(daily=1, monthly=1, enhanced=1)
NOTHING = Delta()


@dataclass(frozen=True)
class Case:
    id: str
    tier: Tier
    enhanced: bool
    # What the provider does (tests/limits/_harness.py OUTCOMES / ERRORS);
    # None for a refusal before the provider (it must not be reached).
    provider: str | None
    status: int
    code: str | None
    delta: Delta
    spend: str
    start: Start = field(default_factory=Start)
    settings: dict[str, Any] = field(default_factory=dict)


# fmt: off
CASES: list[Case] = [
    # ---- Free, standard scan: charged whenever the provider answered -----
    Case("free-standard-books_found-charged", FREE, False, "books",
         ALLOWED, None, CHARGED, COST),
    Case("free-standard-zero_books-charged", FREE, False, "no_books",
         ALLOWED, None, CHARGED, COST),
    Case("free-standard-provider_refused-charged", FREE, False, "refused",
         ALLOWED, None, CHARGED, COST),
    Case("free-standard-truncated-charged", FREE, False, "truncated",
         ALLOWED, None, CHARGED, COST),
    Case("free-standard-last_daily_unit-charged", FREE, False, "books",
         ALLOWED, None, CHARGED, COST, Start(daily=4, monthly=4)),
    # ---- Free, standard: provider down -> units refunded -----------------
    Case("free-standard-unavailable-refunded", FREE, False, "unavailable",
         503, "scan_unavailable", NOTHING, NO_SPEND),
    Case("free-standard-connect_error-refunded", FREE, False, "connect_error",
         503, "scan_unavailable", NOTHING, NO_SPEND),
    Case("free-standard-claude_connect_error-refunded", FREE, False,
         "claude_connect_error", 503, "scan_unavailable", NOTHING, NO_SPEND),
    # A timeout is refunded to the user but likely billed: the estimate
    # stays as spend.
    Case("free-standard-timeout-refunded_estimate_spent", FREE, False, "timeout",
         503, "scan_unavailable", NOTHING, ESTIMATE),
    # Any other error after the reservation keeps it (may have been paid).
    Case("free-standard-other_error-kept", FREE, False, "error",
         500, "RuntimeError", CHARGED, ESTIMATE),
    # ---- Free, standard: refused before the provider ---------------------
    Case("free-standard-daily_limit-refused", FREE, False, None,
         429, "daily_scan_limit", NOTHING, NO_SPEND, Start(daily=LIMIT)),
    Case("free-standard-zero_daily_allowance-refused", FREE, False, None,
         429, "daily_scan_limit", NOTHING, NO_SPEND,
         settings={"quota.free.daily": 0}),
    Case("free-standard-monthly_limit-refused", FREE, False, None,
         402, "scan_limit_reached", NOTHING, NO_SPEND, Start(monthly=LIMIT)),
    Case("free-standard-daily_and_monthly_limit-daily_wins", FREE, False, None,
         429, "daily_scan_limit", NOTHING, NO_SPEND,
         Start(daily=LIMIT, monthly=LIMIT)),
    Case("free-standard-global_cap-refused", FREE, False, None,
         503, "scan_capacity", NOTHING, NO_SPEND, Start(global_others=LIMIT),
         {"scan.global_daily_limit": 3}),
    Case("free-standard-spend_ceiling-refused", FREE, False, None,
         503, "ai_budget_reached", NOTHING, NO_SPEND, Start(spend=LIMIT)),
    Case("free-standard-zero_ceiling-refused", FREE, False, None,
         503, "ai_budget_reached", NOTHING, NO_SPEND,
         settings={"breaker.free_daily_ceiling_micros": 0}),
    Case("free-standard-global_cap_and_ceiling-global_wins", FREE, False, None,
         503, "scan_capacity", NOTHING, NO_SPEND,
         Start(global_others=LIMIT, spend=LIMIT), {"scan.global_daily_limit": 3}),
    Case("free-standard-scan_in_progress-refused", FREE, False, None,
         429, "scan_in_progress", NOTHING, NO_SPEND, Start(lease_held=True)),
    Case("free-standard-in_progress_and_daily_limit-in_progress_wins", FREE,
         False, None, 429, "scan_in_progress", NOTHING, NO_SPEND,
         Start(daily=LIMIT, lease_held=True)),
    Case("free-standard-scanning_disabled-refused", FREE, False, None,
         503, "scan_disabled", NOTHING, NO_SPEND,
         settings={"scan.standard.enabled": False}),
    # ---- Free, enhanced: Premium only, refused before anything -----------
    Case("free-enhanced-requires_premium-refused", FREE, True, None,
         403, "enhanced_requires_premium", NOTHING, NO_SPEND),
    Case("free-enhanced-requires_premium_beats_daily_limit", FREE, True, None,
         403, "enhanced_requires_premium", NOTHING, NO_SPEND, Start(daily=LIMIT)),
    # ---- Premium, standard -----------------------------------------------
    Case("premium-standard-books_found-charged", PREMIUM, False, "books",
         ALLOWED, None, CHARGED, COST),
    Case("premium-standard-zero_books-charged", PREMIUM, False, "no_books",
         ALLOWED, None, CHARGED, COST),
    Case("premium-standard-unavailable-refunded", PREMIUM, False, "unavailable",
         503, "scan_unavailable", NOTHING, NO_SPEND),
    Case("premium-standard-timeout-refunded_estimate_spent", PREMIUM, False,
         "timeout", 503, "scan_unavailable", NOTHING, ESTIMATE),
    Case("premium-standard-other_error-kept", PREMIUM, False, "error",
         500, "RuntimeError", CHARGED, ESTIMATE),
    Case("premium-standard-daily_limit-refused", PREMIUM, False, None,
         429, "daily_scan_limit", NOTHING, NO_SPEND, Start(daily=LIMIT)),
    Case("premium-standard-monthly_limit-refused", PREMIUM, False, None,
         402, "scan_limit_reached", NOTHING, NO_SPEND, Start(monthly=LIMIT)),
    Case("premium-standard-global_cap-refused", PREMIUM, False, None,
         503, "scan_capacity", NOTHING, NO_SPEND, Start(global_others=LIMIT),
         {"scan.global_daily_limit": 3}),
    # The Free spend ceiling never pauses Premium (decision #544).
    Case("premium-standard-past_spend_ceiling-allowed", PREMIUM, False, "books",
         ALLOWED, None, CHARGED, COST, Start(spend=LIMIT)),
    Case("premium-standard-zero_ceiling-allowed", PREMIUM, False, "books",
         ALLOWED, None, CHARGED, COST,
         settings={"breaker.free_daily_ceiling_micros": 0}),
    Case("premium-standard-scan_in_progress-refused", PREMIUM, False, None,
         429, "scan_in_progress", NOTHING, NO_SPEND, Start(lease_held=True)),
    # ---- Premium, enhanced: shared units + one enhanced unit -------------
    Case("premium-enhanced-books_found-charged", PREMIUM, True, "books",
         ALLOWED, None, CHARGED_ENHANCED, COST),
    Case("premium-enhanced-truncated_with_books-charged", PREMIUM, True,
         "truncated", ALLOWED, None, CHARGED_ENHANCED, COST),
    # Zero books below the threshold: the enhanced unit is given back and
    # one zero-result row written; the shared units stay charged.
    Case("premium-enhanced-zero_books_below_threshold-enhanced_given_back",
         PREMIUM, True, "no_books", ALLOWED, None,
         Delta(daily=1, monthly=1, zero_results=1), COST, Start(zero_results=2)),
    # A refusal by the model is a zero-book answer today (#572 may change).
    Case("premium-enhanced-provider_refused_below_threshold-enhanced_given_back",
         PREMIUM, True, "refused", ALLOWED, None,
         Delta(daily=1, monthly=1, zero_results=1), COST),
    Case("premium-enhanced-zero_books_at_threshold_charge-charged", PREMIUM,
         True, "no_books", ALLOWED, None, CHARGED_ENHANCED, COST,
         Start(zero_results=3)),
    Case("premium-enhanced-zero_books_threshold_0_charge-charged", PREMIUM,
         True, "no_books", ALLOWED, None, CHARGED_ENHANCED, COST,
         settings={"scan.enhanced.zero_result_threshold": 0}),
    Case("premium-enhanced-zero_books_below_threshold_throttle-given_back",
         PREMIUM, True, "no_books", ALLOWED, None,
         Delta(daily=1, monthly=1, zero_results=1), COST,
         settings={"scan.enhanced.zero_result_action": "throttle"}),
    Case("premium-enhanced-at_threshold_throttle-refused", PREMIUM, True, None,
         429, "enhanced_throttled", NOTHING, NO_SPEND, Start(zero_results=3),
         {"scan.enhanced.zero_result_action": "throttle"}),
    Case("premium-enhanced-unavailable-all_refunded", PREMIUM, True,
         "unavailable", 503, "scan_unavailable", NOTHING, NO_SPEND),
    Case("premium-enhanced-connect_error-all_refunded", PREMIUM, True,
         "connect_error", 503, "scan_unavailable", NOTHING, NO_SPEND),
    Case("premium-enhanced-timeout-all_refunded_estimate_spent", PREMIUM, True,
         "timeout", 503, "scan_unavailable", NOTHING, ESTIMATE),
    Case("premium-enhanced-other_error-kept", PREMIUM, True, "error",
         500, "RuntimeError", CHARGED_ENHANCED, ESTIMATE),
    Case("premium-enhanced-enhanced_limit-refused", PREMIUM, True, None,
         402, "enhanced_limit_reached", NOTHING, NO_SPEND, Start(enhanced=LIMIT)),
    Case("premium-enhanced-zero_enhanced_allowance-refused", PREMIUM, True, None,
         402, "enhanced_limit_reached", NOTHING, NO_SPEND,
         settings={"quota.premium.enhanced_monthly": 0}),
    # Standard and enhanced share one daily and monthly allowance (OD-1).
    Case("premium-enhanced-shared_daily_limit-refused", PREMIUM, True, None,
         429, "daily_scan_limit", NOTHING, NO_SPEND, Start(daily=LIMIT)),
    Case("premium-enhanced-shared_monthly_limit-refused", PREMIUM, True, None,
         402, "scan_limit_reached", NOTHING, NO_SPEND, Start(monthly=LIMIT)),
    Case("premium-enhanced-global_cap-refused", PREMIUM, True, None,
         503, "scan_capacity", NOTHING, NO_SPEND, Start(global_others=LIMIT),
         {"scan.global_daily_limit": 3}),
    Case("premium-enhanced-past_spend_ceiling-allowed", PREMIUM, True, "books",
         ALLOWED, None, CHARGED_ENHANCED, COST, Start(spend=LIMIT)),
    Case("premium-enhanced-enhanced_disabled-refused", PREMIUM, True, None,
         503, "enhanced_scan_disabled", NOTHING, NO_SPEND,
         settings={"scan.enhanced.enabled": False}),
    Case("premium-enhanced-scanning_disabled-refused", PREMIUM, True, None,
         503, "scan_disabled", NOTHING, NO_SPEND,
         settings={"scan.standard.enabled": False}),
]
# fmt: on


def _resolve(value: int | str, limit: int) -> int:
    return limit if value == LIMIT else int(value)


@pytest.mark.parametrize("case", [pytest.param(c, id=c.id) for c in CASES])
async def test_limits_matrix(case: Case, limits_db: h.LimitsDb, downstream):
    db = limits_db
    rows = {**BASE, **case.settings}
    tier = case.tier.value
    daily_limit = rows[f"quota.{tier}.daily"]
    monthly_limit = rows[f"quota.{tier}.monthly"]
    global_limit = rows["scan.global_daily_limit"]
    enhanced_limit = rows.get(f"quota.{tier}.enhanced_monthly", 0)
    ceiling = rows["breaker.free_daily_ceiling_micros"]
    est = h.estimate(case.enhanced)

    day = db.make_day()
    month = quota.month_start(day)
    user_id = await db.make_user()
    other = await db.make_user()

    # -- starting counters ------------------------------------------------
    s = case.start
    for uid, feature, kind, start, units in (
        (user_id, h.SCAN, "day", day, _resolve(s.daily, daily_limit)),
        (user_id, h.SCAN, "month", month, _resolve(s.monthly, monthly_limit)),
        (user_id, h.ENHANCED, "month", month, _resolve(s.enhanced, enhanced_limit)),
        (other, h.SCAN, "day", day, _resolve(s.global_others, global_limit)),
    ):
        await db.seed_units(uid, feature=feature, kind=kind, start=start, units=units)
    spend_start = max(0, ceiling - est + 1) if s.spend == LIMIT else int(s.spend)
    if spend_start:
        await db.seed_spend(day, settled=spend_start)
    await db.seed_zero_results(user_id, s.zero_results)
    held = await db.hold_lease(user_id) if s.lease_held else None

    before = await db.counters(user_id, day)
    h.use_settings(rows)
    provider = h.FakeProvider(case.provider or "books")

    # -- the scan ---------------------------------------------------------
    with h.Clock(h.at(day)).patch(), h.as_tier(case.tier), provider.patch():
        got = await h.call_scan(db.factory, user_id, enhanced=case.enhanced)

    # -- what the client sees ----------------------------------------------
    assert (got.status, got.code) == (case.status, case.code)
    if case.code == "daily_scan_limit":
        # Retry-After: seconds to the next UTC midnight (the clock is noon).
        assert got.headers == {"Retry-After": "43200"}
    if case.code in ("scan_in_progress", "enhanced_throttled"):
        assert int(got.headers["Retry-After"]) > 0
    if case.code in ("scan_limit_reached", "enhanced_limit_reached"):
        limit = monthly_limit if case.code == "scan_limit_reached" else enhanced_limit
        assert got.body == {
            "detail": case.code,
            "limit": limit,
            "used": limit,
            "period_end": quota.next_month_start(day).isoformat(),
        }
    if case.status == ALLOWED and case.enhanced:
        after_enhanced = before.enhanced + case.delta.enhanced
        assert got.response.enhanced_scans_remaining == max(
            0, enhanced_limit - after_enhanced
        )

    # -- the provider -------------------------------------------------------
    reached = case.provider is not None
    assert provider.calls == (1 if reached else 0)
    assert provider.built == (1 if reached else 0)

    # -- counters -----------------------------------------------------------
    after = await db.counters(user_id, day)
    expected = {
        "daily": case.delta.daily,
        "monthly": case.delta.monthly,
        "enhanced": case.delta.enhanced,
        # The service-wide count moves with this user's day unit only.
        "global": case.delta.daily,
        "zero_results": case.delta.zero_results,
    }
    assert after.delta(before) == expected
    assert await db.negative_counters() == 0

    # -- spend: nothing left reserved; settled as the row says --------------
    settled = {
        NO_SPEND: 0,
        ESTIMATE: est,
        COST: h.cost(case.enhanced, rows),
    }[case.spend]
    assert await db.spend(day) == (0, spend_start + settled)

    # -- the lease: released, and another request's lease untouched ---------
    assert await db.lease(user_id) == held


def test_matrix_ids_are_unique_and_cover_every_refusal_code():
    """Every refusal /scan can give today has at least one row."""
    ids = [c.id for c in CASES]
    assert len(ids) == len(set(ids))
    codes = {c.code for c in CASES}
    assert {
        "daily_scan_limit",
        "scan_limit_reached",
        "enhanced_limit_reached",
        "enhanced_requires_premium",
        "enhanced_throttled",
        "scan_in_progress",
        "scan_capacity",
        "ai_budget_reached",
        "scan_disabled",
        "enhanced_scan_disabled",
        "scan_unavailable",
    } <= codes
    outcomes = {c.provider for c in CASES} - {None}
    assert outcomes == set(h.OUTCOMES) | set(h.ERRORS)
