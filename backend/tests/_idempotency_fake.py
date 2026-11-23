"""An in-memory ``Idempotency-Key`` store (#685) for tests without Postgres.

Stands in for ``claim`` / ``complete`` / ``release`` of
``app/services/idempotency.py`` with the same outcomes (a new id, the stored
response, ``KeyReusedError``, ``KeyInProgressError``), keyed by user and key.
Expiry and the pending lease are the real store's (tested in
``tests/unit/test_idempotency_store.py`` and on Postgres)."""

from dataclasses import dataclass, field

from app.services import idempotency as store
from app.services.idempotency import StoredResponse


@dataclass
class FakeRow:
    id: int
    request_hash: str
    state: str = "pending"
    response: StoredResponse | None = None


@dataclass
class FakeStore:
    rows: dict = field(default_factory=dict)
    claims: int = 0
    completes: int = 0
    releases: int = 0
    fail_complete: bool = False
    # Claims whose commit marker is armed; ``mark_committed`` stands in for
    # the endpoint's commit (the real marker is a before_commit listener).
    armed: set = field(default_factory=set)

    async def claim(self, _db, user_id, key, request_hash):
        self.claims += 1
        row = self.rows.get((user_id, key))
        if row is None:
            row = FakeRow(id=len(self.rows) + 1, request_hash=request_hash)
            self.rows[(user_id, key)] = row
            return row.id
        if row.request_hash != request_hash:
            raise store.KeyReusedError
        if row.state == "pending":
            raise store.KeyInProgressError
        if row.state == "committed":
            raise store.KeyOutcomeUnknownError
        return row.response

    def arm_commit_marker(self, _db, record_id):
        self.armed.add(record_id)
        return lambda: self.armed.discard(record_id)

    def mark_committed(self, record_id):
        """What the endpoint's commit does to an armed claim."""
        if record_id in self.armed:
            _, row = self._by_id(record_id)
            row.state = "committed"

    def _by_id(self, record_id):
        return next((k, r) for k, r in self.rows.items() if r.id == record_id)

    async def complete(self, _db, record_id, response):
        self.completes += 1
        if self.fail_complete:
            raise RuntimeError("db down")
        _, row = self._by_id(record_id)
        row.state, row.response = "complete", response

    async def release(self, _db, record_id):
        self.releases += 1
        key, row = self._by_id(record_id)
        if row.state == "pending":  # a committed row is never released
            del self.rows[key]


def install_fake_store(monkeypatch) -> FakeStore:
    """Patch the store's functions with a new :class:`FakeStore`."""
    fake = FakeStore()
    for name in ("claim", "arm_commit_marker", "complete", "release"):
        monkeypatch.setattr(store, name, getattr(fake, name))
    return fake
