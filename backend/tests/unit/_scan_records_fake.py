"""In-memory stand-in for the scan_records reads and writes (#585), with the
same key (user, fingerprint), step and retention semantics and the same
serialisation as app/ai/scan_records.py (only the SQL is replaced)."""

import contextlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from app.ai import scan_records


class FakeScanRecords:
    """In-memory stand-in for the scan_records reads and writes, with the
    same key (user, fingerprint), step and retention semantics and the same
    serialisation (only the SQL is replaced)."""

    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, str], dict] = {}
        self.calls: list[str] = []

    async def lookup(self, _db, *, user_id, fingerprint, retention_days, now=None):
        self.calls.append("lookup")
        row = self.rows.get((user_id, fingerprint))
        cutoff = (now or scan_records.utc_now()) - timedelta(days=retention_days)
        if row is None or not row["ai_done"] or row["created_at"] <= cutoff:
            return None
        return scan_records.Record(
            feature=row["feature"],
            tier=row["tier"],
            model=row["model"],
            candidates=scan_records.deserialise_candidates(row["candidates"]),
            books=(
                scan_records.deserialise_books(row["enrichment"])
                if row["enrich_done"]
                else None
            ),
            created_at=row["created_at"],
        )

    async def save_ai_step(self, _db, **kw):
        self.calls.append("save_ai_step")
        created = kw.get("now") or scan_records.utc_now()
        self.rows[(kw["user_id"], kw["fingerprint"])] = {
            "feature": kw["feature"],
            "tier": kw["tier"],
            "model": kw["model"],
            "candidates": scan_records.serialise_candidates(kw["candidates"]),
            "enrichment": None,
            "ai_done": True,
            "enrich_done": False,
            "created_at": created,
            "max_per_user": kw["max_per_user"],
        }
        return created

    async def save_enrichment_step(self, _db, **kw):
        self.calls.append("save_enrichment_step")
        row = self.rows.get((kw["user_id"], kw["fingerprint"]))
        if row is None or row["created_at"] != kw["ai_created_at"]:
            return False
        row["enrichment"] = scan_records.serialise_books(kw["books"])
        row["enrich_done"] = True
        return True

    def only(self) -> dict:
        (row,) = self.rows.values()
        return row

    @contextlib.contextmanager
    def installed(self):
        with (
            patch("app.api.scan.scan_records.lookup", self.lookup),
            patch("app.api.scan.scan_records.save_ai_step", self.save_ai_step),
            patch(
                "app.api.scan.scan_records.save_enrichment_step",
                self.save_enrichment_step,
            ),
        ):
            yield self
