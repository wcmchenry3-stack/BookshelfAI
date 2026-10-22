"""In-memory stand-in for the ai_quota / ai_inflight primitives (#528) and
the ai_spend_daily cost breaker primitives (#531)."""

import contextlib
import uuid
from datetime import date
from unittest.mock import patch

from app.ai import breaker, quota


class FakeQuotaStore:
    """In-memory stand-in for ai_quota and ai_inflight with the same semantics
    as the primitives in app/ai/quota.py (#528)."""

    def __init__(self, default_user: uuid.UUID | None = None) -> None:
        self.default_user = default_user
        self.rows: dict[tuple[uuid.UUID, str, str, date], int] = {}
        self.leases: dict[uuid.UUID, uuid.UUID] = {}
        self.expired: set[uuid.UUID] = set()
        self.extra_global = 0
        self.releases = 0
        # ai_spend_daily (#531): day -> [reserved, settled, alerted_mask]
        self.spend: dict[date, list[int]] = {}
        self.cost_refusals = 0
        # ai_enhanced_zero_results (#533): user_id -> rows' ledger ids, all
        # treated as inside the window.
        self.zero_results: dict[uuid.UUID, list[uuid.UUID | None]] = {}

    async def global_units_today(self, _db, day: date) -> int:
        return self.extra_global + sum(
            n for (_, _, kind, d), n in self.rows.items() if kind == "day" and d == day
        )

    async def acquire_lease(self, _db, user_id, seconds) -> uuid.UUID | None:
        assert seconds > 0
        if user_id in self.leases and user_id not in self.expired:
            return None
        self.expired.discard(user_id)
        self.leases[user_id] = lease_id = uuid.uuid4()
        return lease_id

    async def renew_lease(self, _db, user_id, lease_id, seconds) -> bool:
        assert seconds > 0
        if self.leases.get(user_id) != lease_id:
            return False
        self.expired.discard(user_id)
        return True

    async def release_lease(self, _db, user_id, lease_id) -> bool:
        self.releases += 1
        if self.leases.get(user_id) == lease_id:
            del self.leases[user_id]
            return True
        return False

    async def reserve_unit(self, _db, user_id, feature, kind, start, limit):
        key = (user_id, feature, kind, start)
        current = self.rows.get(key, 0)
        if limit <= 0 or current >= limit:
            return None
        self.rows[key] = current + 1
        return current + 1

    async def refund_unit(self, _db, user_id, feature, kind, start) -> None:
        key = (user_id, feature, kind, start)
        if key in self.rows:
            self.rows[key] = max(0, self.rows[key] - 1)

    async def units_used(self, _db, user_id, feature, kind, start) -> int:
        return self.rows.get((user_id, feature, kind, start), 0)

    # --- zero-result enhanced scans (#533), same decisions as app/ai/quota.py

    async def zero_result_throttle(self, _db, user_id, threshold, window_hours):
        count = len(self.zero_results.get(user_id, []))
        if count < threshold:
            return None
        return quota.retry_after_seconds(None, window_hours)

    async def record_zero_result(self, _db, reservation, ledger_id):
        rows = self.zero_results.setdefault(reservation.user_id, [])
        if quota.zero_result_charged(len(rows), reservation.zero_result_threshold):
            return None
        key = (
            reservation.user_id,
            quota.ENHANCED_QUOTA_FEATURE,
            "month",
            reservation.month,
        )
        if key in self.rows:
            self.rows[key] = max(0, self.rows[key] - 1)
        rows.append(ledger_id)
        return len(rows)

    # --- cost breaker (#531), same semantics as app/ai/breaker.py ---

    async def spend_reserve(self, _db, day, est, ceiling, *, enforce) -> bool:
        row = self.spend.get(day, [0, 0, 0])
        if enforce and row[0] + row[1] + est > ceiling:
            self.cost_refusals += 1
            return False
        row[0] += est
        self.spend[day] = row
        return True

    async def spend_release(self, _db, day, est) -> None:
        if day in self.spend:
            self.spend[day][0] = max(0, self.spend[day][0] - est)

    async def spend_settle(self, _db, day, est, cost):
        if day not in self.spend:
            return None
        row = self.spend[day]
        row[0] = max(0, row[0] - est)
        row[1] += cost
        return (row[1], row[0], row[2])

    async def spend_claim_alerts(self, _db, day, pcts) -> list[int]:
        won = []
        for pct in pcts:
            bit = breaker.threshold_bit(pct)
            if day in self.spend and not self.spend[day][2] & bit:
                self.spend[day][2] |= bit
                won.append(pct)
        return won

    def spend_today(self) -> tuple[int, int]:
        """``(reserved, settled)`` for today."""
        reserved, settled, _ = self.spend.get(quota.utc_today(), [0, 0, 0])
        return reserved, settled

    def count(self, user_id=None, feature=quota.SCAN_QUOTA_FEATURE, kind="day") -> int:
        user_id = user_id or self.default_user
        today = quota.utc_today()
        start = today if kind == "day" else quota.month_start(today)
        return self.rows.get((user_id, feature, kind, start), 0)

    def patches(self):
        names = (
            "global_units_today",
            "acquire_lease",
            "renew_lease",
            "release_lease",
            "reserve_unit",
            "refund_unit",
            "units_used",
            "zero_result_throttle",
            "record_zero_result",
        )
        spend = {
            "reserve": self.spend_reserve,
            "release": self.spend_release,
            "settle": self.spend_settle,
            "claim_alerts": self.spend_claim_alerts,
        }
        return [patch.object(quota, n, getattr(self, n)) for n in names] + [
            patch.object(breaker, n, fn) for n, fn in spend.items()
        ]

    @contextlib.contextmanager
    def installed(self):
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            yield self

    def expire_lease(self, user_id) -> None:
        self.expired.add(user_id)
