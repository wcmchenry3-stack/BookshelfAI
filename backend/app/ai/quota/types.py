"""Allowances, refusals and the values the quota steps hand each other.

No imports from the other quota modules but the constants, so every
module can import these."""

import uuid
from dataclasses import dataclass
from datetime import date

from app.ai.entitlements import Tier
from app.ai.quota.constants import FLAG_DAILY, IN_PROGRESS_RETRY_AFTER_SECONDS
from app.ai.settings import AISettings
from app.models.ai_quota import WINDOW_DAY


@dataclass(frozen=True)
class Allowance:
    """The tier's per-user allowances. Daily only: there is no monthly
    allowance (#582). ``daily`` is the found counter's ceiling (#577: every
    scan reserves a found unit); ``no_match`` the no-match ceiling."""

    daily: int
    no_match: int = 0


def allowance_for(ai: AISettings, tier: Tier) -> Allowance:
    """The tier's allowances from ``ai_settings`` (validated, bounded)."""
    return Allowance(
        daily=ai.daily_allowance(tier.value),
        no_match=ai.no_match_allowance(tier.value),
    )


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class QuotaRefusal(Exception):
    """A scan was refused before the provider; nothing is left reserved."""


class GlobalCapacityReached(QuotaRefusal):
    """The service-wide daily ceiling is used up (503 ``scan_capacity``)."""


class CostCeilingReached(QuotaRefusal):
    """Today's AI spend is at the Free daily ceiling (503 ``ai_budget_reached``)."""


class ScanInProgress(QuotaRefusal):
    """Another paid call for this user holds the lease (429 ``scan_in_progress``)."""

    retry_after = IN_PROGRESS_RETRY_AFTER_SECONDS


class DailyLimitReached(QuotaRefusal):
    """The user's daily allowance is used (429 ``daily_scan_limit``)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("daily_scan_limit")
        self.retry_after = retry_after


class ScanRequestsDailyLimitReached(QuotaRefusal):
    """The user has made today's allowed number of /scan requests (#586,
    429 ``scan_requests_daily_limit``); resets at UTC midnight."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("scan_requests_daily_limit")
        self.retry_after = retry_after


class EnhancedRequiresPremium(QuotaRefusal):
    """Enhanced scans are Premium-only (403 ``enhanced_requires_premium``)."""


class EnhancedThrottled(QuotaRefusal):
    """Too many uncharged zero-result enhanced scans in the window, with
    action ``throttle`` (429 ``enhanced_throttled``)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("enhanced_throttled")
        self.retry_after = retry_after


@dataclass(frozen=True)
class ZeroResult:
    """Passed to :func:`release` when an enhanced scan's provider answered but
    returned no books, i.e. no candidates (#533). ``ledger_id`` is the usage-ledger
    ``call_id`` (None when the settle has no ledger row)."""

    ledger_id: uuid.UUID | None = None


@dataclass(frozen=True)
class NoMatch:
    """Passed to :func:`release` when a standard scan's provider answered
    properly (outcome ``ok``) with no candidates (#577): the settle moves the
    found unit to the no-match counter when it has room."""


@dataclass(frozen=True)
class NoMatchMoved:
    """What the no-match decision did: ``outcome`` is :data:`NO_MATCH_FREE`
    (moved: no found unit used) or :data:`NO_MATCH_COUNTED` (no-match was
    full: the found unit stays charged); ``count`` is the user's no-match
    count after it; ``warn`` whether this move brought it to the ceiling
    (the ceiling warning's claim)."""

    outcome: str
    count: int
    warn: bool = False


@dataclass(frozen=True)
class Reservation:
    """What :func:`reserve` took; hand it to :func:`refund` and :func:`release`."""

    user_id: uuid.UUID
    feature: str
    lease_id: uuid.UUID
    day: date
    tier: Tier
    allowance: Allowance
    # The cost estimate added to ai_spend_daily for ``day`` (#531), and the
    # Free ceiling read for this request (for the alert percentages).
    est_cost_micros: int = 0
    ceiling_micros: int = 0
    # Enhanced scans (#533): whether this is one, and the zero-result
    # settings read for this request (one view of ai_settings per request).
    enhanced: bool = False
    zero_result_threshold: int = 0
    zero_result_window_hours: int = 24
    zero_result_action: str = "charge"


@dataclass(frozen=True)
class ZeroResultRecorded:
    """An uncharged zero-result scan was recorded: the user's new in-window
    count, and whether this row claimed the threshold warning."""

    count: int
    warn: bool = False


@dataclass(frozen=True)
class _Unit:
    """One unit a scan takes: its lease-row flag and its ai_quota key."""

    flag: str
    feature: str
    window_kind: str
    window_start: date
    what: str


def _daily_unit(reservation: Reservation) -> _Unit:
    """The daily unit of ``reservation``: the only unit a scan takes (#582)."""
    return _Unit(
        FLAG_DAILY,
        reservation.feature,
        WINDOW_DAY,
        reservation.day,
        "daily",
    )
