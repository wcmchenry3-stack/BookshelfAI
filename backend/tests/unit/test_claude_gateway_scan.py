"""Claude provider through the gateway and /scan (#532).

``scan.<feature>.provider = anthropic`` in ai_settings routes the call through
``AnthropicVisionProvider``; the SDK client is mocked (``tests._claude_fake``),
never a real model. /scan records the usage ledger row (incl. Claude's cache
write tokens) and refunds exactly as for OpenAI.
"""

import io
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.dialects import postgresql

from app.ai import ledger
from app.ai import settings as ai_settings
from app.ai.gateway import VisionPayload, _build_provider, run_feature
from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
from app.ai.providers.anthropic_vision import AnthropicVisionProvider
from app.ai.registry import FEATURES
from app.main import app
from app.models.user import User
from tests._claude_fake import (
    connect_error,
    fake_claude,
    message,
    status_error,
)
from tests.unit._quota_fake import FakeQuotaStore

USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-000000000532"),
    email="claude@example.com",
    enhanced_scan_credits=3,
)
ANTHROPIC = {
    "scan.standard.provider": "anthropic",
    "scan.standard.model": "claude-haiku-5-5",
    "scan.enhanced.provider": "anthropic",
    "scan.enhanced.model": "claude-sonnet-5-5",
}
DUNE = {
    "title": "Dune",
    "author": "Frank Herbert",
    "confidence": 0.9,
    "isbn_13": "9780441013593",
    "isbn_10": None,
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (5, 99, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _file():
    return {"file": ("scan.jpg", io.BytesIO(_jpeg()), "image/jpeg")}


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feature", "model"),
    [("scan.standard", "claude-haiku-5-5"), ("scan.enhanced", "claude-sonnet-5-5")],
)
async def test_gateway_routes_anthropic_through_the_adapter(feature, model):
    reply = message(books=[DUNE], model=model, cache_read=300, cache_write=40)
    with fake_claude(reply) as (_cls, create):
        result = await run_feature(
            feature,
            user=MagicMock(),
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"img"),
            settings_snapshot=ai_settings.AISettings(ANTHROPIC),
        )
    assert create.call_args.kwargs["model"] == model
    assert (result.provider, result.model, result.feature) == (
        "anthropic",
        model,
        feature,
    )
    assert [c.title for c in result.candidates] == ["Dune"]
    assert (result.cache_read_tokens, result.cache_write_tokens) == (300, 40)
    assert result.outcome == "ok"


def test_build_provider_picks_the_claude_adapter():
    spec = ai_settings.AISettings(ANTHROPIC).spec_for(FEATURES["scan.standard"])
    assert isinstance(_build_provider(spec), AnthropicVisionProvider)


async def test_default_settings_still_use_openai():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as openai_cls,
        patch("app.ai.gateway.AnthropicVisionProvider") as claude_cls,
    ):
        openai_cls.return_value.identify = AsyncMock(
            return_value=MagicMock(
                candidates=[],
                outcome="ok",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
        )
        await run_feature(
            "scan.standard",
            user=MagicMock(),
            db=MagicMock(),
            payload=VisionPayload(image_bytes=b"img"),
            settings_snapshot=ai_settings.AISettings({}),
        )
    openai_cls.assert_called_once()
    claude_cls.assert_not_called()


# ---------------------------------------------------------------------------
# /scan: ledger and refunds
# ---------------------------------------------------------------------------


@pytest.fixture
def store():
    s = FakeQuotaStore(default_user=USER.id)
    with s.installed():
        yield s


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    ai_settings.reset_cache(ANTHROPIC)
    with (
        patch("app.api.scan.EnrichmentService") as enrich,
        patch("app.api.scan.DeduplicationService") as dedup,
    ):
        enrich.return_value.enrich = AsyncMock(return_value=[])
        dedup.return_value.check = AsyncMock(return_value=[])
        yield TestClient(app)
    app.dependency_overrides.clear()
    ai_settings.reset_cache()


@pytest.fixture
def rows():
    built: list[dict] = []
    real_insert = ledger.usage_insert

    def spy_insert(**kwargs):
        stmt = real_insert(**kwargs)
        built.append({**kwargs, "stmt": stmt})
        return stmt

    with patch.object(ledger, "usage_insert", spy_insert):
        yield built


def _cost(stmt) -> int:
    return stmt.compile(dialect=postgresql.dialect()).params["cost_micros"]


def test_claude_scan_records_cache_tokens_and_prices_them(client, store, rows):
    reply = message(
        books=[DUNE],
        input_tokens=2_800,
        output_tokens=150,
        cache_read=0,
        cache_write=600,
    )
    with fake_claude(reply):
        assert client.post("/scan", files=_file()).status_code == 200
    [row] = rows
    assert (row["feature"], row["model"], row["status"]) == (
        "scan.standard",
        "claude-haiku-5-5",
        "ok",
    )
    assert (row["cache_read_tokens"], row["cache_write_tokens"]) == (0, 600)
    expected = cost_micros(
        "claude-haiku-5-5",
        CURRENT_PRICE_VERSION,
        input_tokens=2_800,
        output_tokens=150,
        cache_write_tokens=600,
    )
    # 2,800 * 0.10 + 150 * 0.50 + 600 * 0.125 = 280 + 75 + 75 = 430 micros
    assert expected == 430
    assert _cost(row["stmt"]) == expected
    assert store.count() == 1


def test_claude_refusal_records_refused_and_keeps_the_reservation(client, store, rows):
    with fake_claude(message(books=[DUNE], stop_reason="refusal")):
        resp = client.post("/scan", files=_file())
    assert resp.status_code == 200
    assert resp.json()["books"] == []
    [row] = rows
    assert row["status"] == "refused"
    assert row["input_tokens"] == 1_200  # billed, so recorded
    assert store.count() == 1  # kept, as for any empty result (#509)


def test_claude_truncated_records_truncated(client, store, rows):
    # A cut-off reply that still holds a complete book (#572: one with no
    # book is malformed, see the test below).
    cut = '{"books": [' + json.dumps(DUNE) + ', {"title": "Du'
    with fake_claude(message(cut, stop_reason="max_tokens")):
        assert client.post("/scan", files=_file()).status_code == 200
    assert rows[0]["status"] == "truncated"


def test_claude_truncated_with_no_books_is_malformed_and_refunded(client, store, rows):
    """Owner decision on #572: a cut-off reply never counts against the user."""
    with fake_claude(message('{"books": [{"title": "Du', stop_reason="max_tokens")):
        resp = client.post("/scan", files=_file())
    assert (resp.status_code, resp.json()["detail"]) == (503, "scan_unavailable")
    [row] = rows
    assert row["status"] == "malformed"
    assert row["input_tokens"] == 1_200  # billed, so recorded
    assert store.count() == 0  # refunded


@pytest.mark.parametrize(
    "replies",
    [
        (connect_error(),),
        (status_error(400),),
        (status_error(529), status_error(529), status_error(529)),
    ],
    ids=["never-connected", "bad-request", "overloaded-after-retries"],
)
def test_claude_unavailable_is_refunded(client, store, rows, replies):
    with (
        patch("app.ai.providers.anthropic_vision._sleep", new=AsyncMock()),
        fake_claude(*replies),
    ):
        resp = client.post("/scan", files=_file())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "scan_unavailable"}
    [row] = rows
    assert row["status"] == "refunded"
    assert _cost(row["stmt"]) == 0
    assert store.count() == 0


def test_missing_key_is_unavailable_not_a_crash(client, store, rows):
    with fake_claude(message(), api_key="") as (client_cls, _create):
        resp = client.post("/scan", files=_file())
    assert resp.status_code == 503
    assert resp.json() == {"detail": "scan_unavailable"}
    client_cls.assert_not_called()
    assert rows[0]["status"] == "refunded"
    assert store.count() == 0
