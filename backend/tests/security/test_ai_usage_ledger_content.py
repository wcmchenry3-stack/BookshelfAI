"""The usage ledger never stores content (#530): no prompt, image bytes or raw
model output, and no client-supplied fields. Asserts the exact column sets,
so adding any column needs a deliberate change here.

Run on its own with:  pytest -m security tests/security/test_ai_usage_ledger_content.py
"""

import ast
import inspect

import pytest
from sqlalchemy import LargeBinary, Text

from app.ai import ledger
from app.models.ai_usage import AIUsage, AIUsageDaily

pytestmark = pytest.mark.security

FORBIDDEN_FRAGMENTS = (
    "prompt",
    "image",
    "content",
    "raw",
    "response",
    "output_text",
    "message",
    "text",
    "body",
    "bytes",
    "email",
)


def test_ai_usage_columns_are_exactly_counts_and_metadata():
    assert {c.name for c in AIUsage.__table__.columns} == {
        "id",
        "call_id",
        "user_id",
        "feature",
        "model",
        "price_version",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_micros",
        "status",
        "request_id",
        "created_at",
    }


def test_ai_usage_daily_columns_are_exactly_totals():
    assert {c.name for c in AIUsageDaily.__table__.columns} == {
        "day",
        "feature",
        "model",
        "status",
        "calls",
        "input_tokens",
        "output_tokens",
        "cost_micros",
    }


@pytest.mark.parametrize("table", [AIUsage.__table__, AIUsageDaily.__table__])
def test_no_content_shaped_columns(table):
    for column in table.columns:
        # "output_tokens" is a count, not output: only flag non-count columns.
        if column.name.endswith("_tokens"):
            continue
        for fragment in FORBIDDEN_FRAGMENTS:
            assert fragment not in column.name, column.name
        # No column that could hold a blob or a long text.
        assert not isinstance(column.type, LargeBinary | Text), column.name
        length = getattr(column.type, "length", None)
        assert length is None or length <= 64, column.name


def test_ledger_never_emits_per_call_sentry_events():
    """Sentry tags for feature/model/status belong on the breaker event
    (#531), not on every call."""
    tree = ast.parse(inspect.getsource(ledger))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names
            if isinstance(node, ast.Import)
            else [ast.alias(name=node.module or "")]
        )
    }
    assert "sentry_sdk" not in imported


def test_call_id_is_server_generated_in_scan():
    """call_id comes from uuid.uuid4() in /scan, never from the request."""
    from app.api import scan

    source = inspect.getsource(scan.scan)
    assert "call_id = uuid.uuid4()" in source


def test_usage_insert_accepts_no_content_arguments():
    params = set(inspect.signature(ledger.usage_insert).parameters)
    assert params == {
        "call_id",
        "user_id",
        "feature",
        "model",
        "status",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "request_id",
        "price_version",
    }
