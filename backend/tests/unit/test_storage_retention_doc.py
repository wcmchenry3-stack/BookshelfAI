"""Drift guard for docs/storage-and-retention.md (#576).

Every table in ``app.models`` must be named in the page's retention table, and
the retention numbers the page states must match the purge constants.
"""

from pathlib import Path

import pytest

import app.models  # noqa: F401  (registers every model on Base.metadata)
from app.ai import breaker, ledger, quota
from app.core.database import Base

DOC = Path(__file__).resolve().parents[3] / "docs" / "storage-and-retention.md"
HOW_TO_FIX = (
    "add a row for it to the retention table in docs/storage-and-retention.md "
    "(what it holds, retention rule, purge, whether the data-model doc matches) "
    "and, if it grows with usage, a purge in app/services/token_cleanup.py"
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_every_model_table_is_in_the_retention_doc(table, doc_text):
    assert f"| `{table}`" in doc_text, (
        f"table {table!r} is not in the retention table of "
        f"docs/storage-and-retention.md: {HOW_TO_FIX}"
    )


def test_documented_retention_numbers_match_the_code(doc_text):
    assert ledger.RAW_RETENTION_DAYS == 30
    assert ledger.DAILY_RETENTION_DAYS == 400
    assert quota.DAILY_RETENTION_DAYS == 90
    assert quota.MONTHLY_RETENTION_MONTHS == 13
    assert quota.ZERO_RESULT_RETENTION_DAYS == 30
    assert breaker.RETENTION_DAYS == 90
    for batch in (ledger, quota, breaker):
        assert batch.PURGE_BATCH_SIZE == 5_000
        assert batch.PURGE_MAX_BATCHES == 20
    # The page says these in words; change both together.
    for phrase in ("30 days", "400 days", "90 days", "13 months", "5,000 rows"):
        assert phrase in doc_text, f"{phrase!r} no longer appears in the doc"
