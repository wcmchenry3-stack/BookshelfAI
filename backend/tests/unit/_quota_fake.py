"""In-memory stand-in for the ai_quota / ai_inflight primitives (#528)."""

import contextlib
import uuid
from datetime import date
from unittest.mock import patch

from app.ai import quota


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
        )
        return [patch.object(quota, n, getattr(self, n)) for n in names]

    @contextlib.contextmanager
    def installed(self):
        with contextlib.ExitStack() as stack:
            for p in self.patches():
                stack.enter_context(p)
            yield self

    def expire_lease(self, user_id) -> None:
        self.expired.add(user_id)
