"""Per-user AI allowances: reserve before the provider, settle or refund after.

Every paid scan goes through :func:`reserve` before the provider is called.
In order (#528):

1. **Entitlement.** The tier comes from ``entitlements.tier_for(db, user)``
   (server-side only); the allowances come from ``ai_settings``
   (``quota.<tier>.found_daily`` and ``quota.<tier>.no_match_daily``, #577).
   Nothing in the request can change either.
2. **Global ceiling** (``scan.global_daily_limit``, approximate). Today's day
   rows are summed across users and features and compared before anything is
   reserved, so no per-user row or lease is taken when the service-wide
   capacity is gone (:class:`GlobalCapacityReached`). Concurrent requests can
   overshoot by at most the number in flight; the lease bounds that to one per
   user.
3. **Daily cost ceiling** (#531, ``app/ai/breaker.py``). The feature's
   estimate (``est_cost_micros``) is added to today's ``ai_spend_daily`` row
   with one conditional upsert. For Free it succeeds only while the day's
   reserved plus settled spend stays within
   ``breaker.free_daily_ceiling_micros`` (:class:`CostCeilingReached`, 503
   ``ai_budget_reached``); Premium always adds its estimate (decision #544). It
   sits before the lease, so nothing per-user is taken when the service is
   out of budget, and every later refusal or error gives it back.
4. **In-flight lease** (table ``ai_inflight``). One conditional upsert:
   ``INSERT ... ON CONFLICT (user_id) DO UPDATE ... WHERE
   ai_inflight.lease_expires_at < clock_timestamp()``. It succeeds only when
   the user has no row or the old lease has expired, so at most one paid call
   per user is in flight (:class:`ScanInProgress`). The lease lasts
   ``timeout_s + 30`` seconds, so a crashed request frees it. Each lease has a
   random ``lease_id``; :func:`release` deletes the row only while it still
   holds that id, so a slow request whose lease expired and was taken over
   cannot delete the new holder's lease. After the reservation below the
   lease is renewed (:func:`renew_lease`), so the provider call gets the full
   lease; if a stall let another request take it over, the reservation is
   given back and :class:`ScanInProgress` raised.
5. **Daily reservation** (table ``ai_quota``). The same exact conditional
   upsert as #509: ``units_used = units_used + 1 WHERE units_used < limit``.
   Postgres serialises upserts on the same key, so no more than ``limit``
   reservations can succeed (:class:`DailyLimitReached`). A limit of 0 refuses
   without writing. Standard and enhanced scans share one allowance (OD-1):
   both reserve under :data:`SCAN_QUOTA_FEATURE`. The ``feature`` column stays
   so later AI features can have their own allowance.

**No monthly limit (#582).** There is no monthly allowance for any tier, and
no Premium enhanced monthly allowance: nothing here takes, checks or gives
back a month-window unit, and no scan is refused for a monthly reason. What
bounds a month is the daily allowance of each user (at most 31 x the daily
limit), the service-wide ``scan.global_daily_limit``, the daily spend ceiling
(``breaker.free_daily_ceiling_micros``) and, outside the code, the monthly
spend limit in the provider console (#542). Spend and call counts per month
stay derivable from the usage ledger (``ai_usage`` / ``ai_usage_daily``,
``app/ai/ledger.py``): tracking, not a limit. Month rows (``window_kind =
'month'``) written before #582 are no longer read or written; the retention
purge below removes them as they age past 13 months (kept so a rollback to
the previous release finds its counters intact).

**Unit flag (#590).** The daily unit is taken by :func:`take_unit`: the
``ai_quota`` increment and a flag on the request's own lease row
(``ai_inflight.daily_taken``, matched on ``lease_id``) commit together; if the
lease is no longer ours nothing is taken (:class:`ScanInProgress`). So after a
commit whose acknowledgement was lost, the flag shows whether the unit
landed. Every give-back of the unit (:func:`refund`, and the ``except`` in
:func:`reserve`, which covers a lost acknowledgement on a take) goes through
:func:`give_back_unit`: it clears the flag and takes the unit off in one
commit, only if the flag was set, so a retry gives nothing back twice; the
give-backs are therefore retried like the others (#567 had made them
one-shot when nothing gated them). The settle in :func:`release` needs no
flag: it deletes the lease row, flag and all. If another request took over
the expired lease, the takeover resets the flags (they belong to the new
lease): a unit this request knows it took (its take was acknowledged) is then
given back once without the gate, on the first attempt only; a unit whose
take was never acknowledged stays charged (fails toward charging). The
``monthly_taken`` and ``enhanced_taken`` columns no longer exist (dropped by
migration 0018, #594).

**Found and no-match counters (#577).** Each user has two daily counters:
``found`` (:data:`SCAN_QUOTA_FEATURE`, the daily unit above: every scan takes
one before the provider, so a scan is refused with 429 ``daily_scan_limit``
when found is full, even if no-match has room, since the next photo may find
books) and ``no_match`` (:data:`SCAN_NO_MATCH_QUOTA_FEATURE`). When a
**standard** scan's provider answered properly (outcome ``ok``) with no
candidates, the caller passes a :class:`NoMatch` to :func:`release`, and in
the settle transaction (the same gate as the cost settle and the #566
zero-result record: the ledger insert, or the lease, so a retried settle
never moves twice) :func:`move_to_no_match` moves the unit: one no-match unit
is taken (``units_used < no_match ceiling``, the same exact conditional
upsert) and the found unit given back, both under the lock of the request's
own lease row and only while its ``daily_taken`` flag is set (cleared in the
same transaction). If no-match is full, the found unit stays charged
(``counted``). The move runs in a SAVEPOINT: a failure there keeps the scan
charged and never breaks the settle or the response. Enhanced scans never
move (the #533 zero-result rule stays for them), nor do malformed (already
refunded), refused or truncated replies. The service-wide count sums every
day row, so it still counts every provider call, found or no-match. So a
user's provider calls per day never exceed found + no-match ceilings. The
move that brings no-match to its ceiling also logs a line and, after the
commit, sends one Sentry warning (:func:`send_no_match_ceiling_warning`):
counts only rise within a day, so that is at most once per user per day
(per ceiling value: raising the ceiling mid-day lets the new one be reached
once more). After a lost commit acknowledgement the retry does not move and
sends nothing (fewer alerts, never more).

**/scan requests per user per day (#586).** Before any of the steps above,
and before the scan-record lookup, ``/scan`` calls
:func:`count_scan_request`: one more request under
:data:`SCAN_REQUESTS_QUOTA_FEATURE` for the user's UTC day, with the same
exact conditional upsert (``units_used < scan.requests_per_user_daily``),
or :class:`ScanRequestsDailyLimitReached` (429
``scan_requests_daily_limit``, Retry-After to UTC midnight) with nothing
written. It counts every request past authentication and the kill
switches, new photo or repeat, allowed or refused later, and nothing is
ever given back. It is the abuse control for repeats, which are free
(decision #573 point 3). It counts requests, not AI calls, so
:func:`global_units_today` leaves the key out
(:data:`NON_AI_QUOTA_FEATURES`); the rows are day rows, purged with the
others.

**Enhanced scans (#533).** ``reserve(..., enhanced=True)`` adds two steps:

- right after the tier is resolved, a non-Premium tier is refused
  (:class:`EnhancedRequiresPremium`, 403 ``enhanced_requires_premium``) before
  anything is taken (no cost estimate, lease or unit);
- after the lease, with ``scan.enhanced.zero_result_action`` = ``throttle``,
  the user's zero-result enhanced scans recorded inside the sliding window
  (``ai_enhanced_zero_results``) are counted; at or above
  ``scan.enhanced.zero_result_threshold`` the scan is refused
  (:class:`EnhancedThrottled`, 429 ``enhanced_throttled``) with a Retry-After
  of the seconds until enough rows leave the window. Counting under the lease
  serialises it with the user's own settle.

An enhanced scan takes the shared daily unit and nothing else (#582: the
Premium enhanced monthly allowance is gone; a per-model daily Sonnet counter
comes with #583). The zero-result rule stays as the abuse signal: when the
provider answered but returned no books (no candidates), the caller passes a
:class:`ZeroResult` to :func:`release`, and in the settle transaction (gated
exactly like the cost settle, so a retried settle cannot apply it twice) the
user's in-window count is read: below the threshold one
``ai_enhanced_zero_results`` row is written; at or above it nothing is
written. The daily unit stays charged either way (the provider answered).
The write runs in a SAVEPOINT, so a failure there never drops the usage row.
When a row brings the count to the threshold and no row of the user with
``warned = true`` is inside the window, that row is written with ``warned =
true`` (the claim commits with it) and one Sentry warning is sent after the
commit (:func:`send_zero_result_warning`): at most one per user per window.
Threshold 0: with ``charge`` no rows and no warning; with ``throttle`` every
enhanced scan is refused. A repeat served from a scan record (#585) makes no
provider call and must not pass a :class:`ZeroResult`.

Every step commits straight away, on the request's own session: the
reservation is visible to concurrent requests and the connection goes back to
the pool before the provider is called (as in #509/#519). A refusal or an
error at any step gives back what was already taken (the cost estimate, the
daily reservation, the lease) before it propagates.

After the provider call the caller must always :func:`release` the lease (in a
``finally``, even on cancellation; the release is shielded). The caller passes
the usage-ledger row for the call (#530) and the call's
:class:`~app.ai.breaker.Settlement` (#531); the ledger row and the cost settle
are committed together with the lease delete. Without a settlement the cost
estimate is given back. On
``ScanUnavailableError`` / ``httpx.ConnectError`` it first calls
:func:`refund`, which gives back the daily unit at most once, never below
zero. Any other outcome (success, an empty result, another error, a cancelled
request) keeps the reservation: the provider may have been paid.

Windows are UTC: the day window starts at UTC midnight.

Retention: :func:`purge_old_quota` deletes day rows older than 90 days and
(legacy, see above) month rows older than 13 months, and
:func:`purge_old_zero_results` deletes ``ai_enhanced_zero_results`` rows older
than 30 days, in bounded batches, from the daily cleanup task.

This package never calls a provider and must not import ``app.ai.providers``.

**Layout (#806).** One module per job; this ``__init__`` re-exports every
name, so callers keep ``from app.ai import quota`` and ``quota.<name>``:

- ``constants``: counter keys, flags, outcomes and tunables;
- ``types``: :class:`Allowance`, the :class:`QuotaRefusal` family,
  :class:`Reservation` and the other values the steps hand each other;
- ``clock``: the UTC windows;
- ``leases``: the in-flight lease (``ai_inflight``);
- ``units``: the ``ai_quota`` units (take, give back, count, global count);
- ``scan_requests``: :func:`count_scan_request` (#586);
- ``zero_result``: the zero-result throttle and record (#533);
- ``no_match``: :func:`move_to_no_match` (#577);
- ``sentry_warnings``: the Sentry warnings;
- ``give_back``: :func:`_give_back`;
- ``settle``: :func:`release` and the spend alerts;
- ``refunds``: :func:`refund` and the gated unit give-back;
- ``reservations``: :func:`reserve`;
- ``usage``: :class:`Usage` and the enhanced scans left;
- ``purge``: retention.

**Calls go through the package.** Inside the package, every call to a
quota function and every read of a tunable number (``CLEANUP_TIMEOUT_SECONDS``,
the purge batches, the retention periods, ``LEASE_GRACE_SECONDS``) is written
``quota.<name>``, looked up on this package at call time, never imported
from the sibling module that defines it. So patching ``app.ai.quota.<name>``
(as the tests do) reaches the code that runs, wherever it lives. Classes,
exceptions and string keys are imported directly: they are never patched,
and ``isinstance``/``except`` see the same class either way.
"""

import logging

from app.ai import alerts, breaker, entitlements
from app.ai.entitlements import Tier
from app.ai.quota.clock import (
    lease_seconds,
    months_before,
    seconds_until_utc_midnight,
    utc_now,
    utc_today,
)
from app.ai.quota.constants import (
    CLEANUP_TIMEOUT_SECONDS,
    DAILY_RETENTION_DAYS,
    ENHANCED_FEATURE_TAG,
    FLAG_DAILY,
    GIVE_BACK_DONE,
    GIVE_BACK_FAILED,
    GIVE_BACK_TIMED_OUT,
    IN_PROGRESS_RETRY_AFTER_SECONDS,
    LEASE_GRACE_SECONDS,
    MONTHLY_RETENTION_MONTHS,
    NO_MATCH_COUNTED,
    NO_MATCH_FREE,
    NON_AI_QUOTA_FEATURES,
    PURGE_BATCH_SIZE,
    PURGE_MAX_BATCHES,
    SCAN_NO_MATCH_QUOTA_FEATURE,
    SCAN_QUOTA_FEATURE,
    SCAN_REQUESTS_QUOTA_FEATURE,
    UNIT_FLAGS,
    UNIT_GIVEN_BACK,
    UNIT_KEPT_FALLBACK_FAILED,
    UNIT_KEPT_GIVE_BACK_FAILED,
    UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED,
    UNIT_LEASE_LOST,
    UNIT_NOT_TAKEN,
    ZERO_RESULT_RETENTION_DAYS,
)
from app.ai.quota.give_back import _give_back
from app.ai.quota.leases import (
    _clock_timestamp,
    _held,
    acquire_lease,
    hold_lease_lock,
    release_lease,
    renew_lease,
)
from app.ai.quota.no_match import move_to_no_match
from app.ai.quota.purge import (
    _purge_kind,
    purge_cutoffs,
    purge_old_quota,
    purge_old_zero_results,
)
from app.ai.quota.refunds import _return_unit, refund
from app.ai.quota.reservations import require_enhanced_entitlement, reserve
from app.ai.quota.scan_requests import count_scan_request
from app.ai.quota.sentry_warnings import (
    _UNIT_TAGS,
    _tier_tag,
    send_no_match_ceiling_warning,
    send_unit_kept_warning,
    send_zero_result_warning,
)
from app.ai.quota.settle import _alert, release
from app.ai.quota.types import (
    Allowance,
    CostCeilingReached,
    DailyLimitReached,
    EnhancedRequiresPremium,
    EnhancedThrottled,
    GlobalCapacityReached,
    NoMatch,
    NoMatchMoved,
    QuotaRefusal,
    Reservation,
    ScanInProgress,
    ScanRequestsDailyLimitReached,
    ZeroResult,
    ZeroResultRecorded,
    _daily_unit,
    _Unit,
    allowance_for,
)
from app.ai.quota.units import (
    _flag_column,
    _unit_upsert,
    give_back_unit,
    global_units_filter,
    global_units_today,
    refund_unit,
    reserve_unit,
    take_unit,
    units_used,
)
from app.ai.quota.usage import (
    Usage,
    enhanced_remaining,
    enhanced_remaining_for,
    usage_for,
)
from app.ai.quota.zero_result import (
    _in_window,
    record_zero_result,
    retry_after_seconds,
    warned_in_window,
    zero_result_charged,
    zero_result_count,
    zero_result_throttle,
)
from app.ai.settings import ZERO_RESULT_ACTION_THROTTLE, AISettings
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import WINDOW_DAY, WINDOW_MONTH, AIInflight, AIQuota
from app.models.user import User

logger = logging.getLogger(__name__)

# Every name app.ai.quota had as one module (#806). Patching any of these on
# the package reaches the code that runs (see "Calls go through the
# package" above). tests/unit/test_quota_package.py guards this list.
__all__ = [
    "CLEANUP_TIMEOUT_SECONDS",
    "DAILY_RETENTION_DAYS",
    "ENHANCED_FEATURE_TAG",
    "FLAG_DAILY",
    "GIVE_BACK_DONE",
    "GIVE_BACK_FAILED",
    "GIVE_BACK_TIMED_OUT",
    "IN_PROGRESS_RETRY_AFTER_SECONDS",
    "LEASE_GRACE_SECONDS",
    "MONTHLY_RETENTION_MONTHS",
    "NON_AI_QUOTA_FEATURES",
    "NO_MATCH_COUNTED",
    "NO_MATCH_FREE",
    "PURGE_BATCH_SIZE",
    "PURGE_MAX_BATCHES",
    "SCAN_NO_MATCH_QUOTA_FEATURE",
    "SCAN_QUOTA_FEATURE",
    "SCAN_REQUESTS_QUOTA_FEATURE",
    "UNIT_FLAGS",
    "UNIT_GIVEN_BACK",
    "UNIT_KEPT_FALLBACK_FAILED",
    "UNIT_KEPT_GIVE_BACK_FAILED",
    "UNIT_KEPT_LEASE_LOST_UNACKNOWLEDGED",
    "UNIT_LEASE_LOST",
    "UNIT_NOT_TAKEN",
    "WINDOW_DAY",
    "WINDOW_MONTH",
    "ZERO_RESULT_ACTION_THROTTLE",
    "ZERO_RESULT_RETENTION_DAYS",
    "_UNIT_TAGS",
    "AIEnhancedZeroResult",
    "AIInflight",
    "AIQuota",
    "AISettings",
    "Allowance",
    "CostCeilingReached",
    "DailyLimitReached",
    "EnhancedRequiresPremium",
    "EnhancedThrottled",
    "GlobalCapacityReached",
    "NoMatch",
    "NoMatchMoved",
    "QuotaRefusal",
    "Reservation",
    "ScanInProgress",
    "ScanRequestsDailyLimitReached",
    "Tier",
    "Usage",
    "User",
    "ZeroResult",
    "ZeroResultRecorded",
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
