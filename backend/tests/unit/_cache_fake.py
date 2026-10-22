"""In-memory stand-in for the ai_result_cache reads and writes (#534), with the
same key, expiry and serialisation as app/ai/cache.py (only the SQL is
replaced)."""

import contextlib
from datetime import datetime
from unittest.mock import patch

from app.ai import cache


class FakeResultCache:
    def __init__(self) -> None:
        # cache_key -> stored row values (as cache.store would write them)
        self.rows: dict[str, dict] = {}
        self.lookups: list[str] = []
        self.stores: list[dict] = []

    async def lookup(self, _db, *, key: str, user_id, now: datetime | None = None):
        self.lookups.append(key)
        row = self.rows.get(key)
        if (
            row is None
            or row["user_id"] != user_id
            or row["expires_at"] <= (now or cache.utc_now())
        ):
            return None
        return cache.deserialise(row["result"])

    async def store(self, _db, **kw) -> None:
        self.stores.append(kw)
        created = kw.get("now") or cache.utc_now()
        self.rows[kw["key"]] = {
            "user_id": kw["user_id"],
            "image_sha256": kw["image_hash"],
            "feature": kw["feature"],
            "model": kw["model"],
            "prompt_version": kw["prompt_version"],
            "result": cache.serialise(kw["candidates"]),
            "created_at": created,
            "expires_at": cache.expires_at(created, kw["ttl_days"]),
        }

    @contextlib.contextmanager
    def installed(self):
        with (
            patch("app.api.scan.cache.lookup", self.lookup),
            patch("app.api.scan.cache.store", self.store),
        ):
            yield self
