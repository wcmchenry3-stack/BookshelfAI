"""The load run's fake provider and setup guards (#539).

The fake must keep speaking the shape the real Claude adapter parses, so the
nightly run exercises the real code path: these tests drive the REAL adapter
(real SDK client, real HTTP) against the fake on a local port. No real model
is reached.
"""

import dataclasses
import json
import threading
import urllib.request
from unittest.mock import patch

import pytest

from app.ai.providers.anthropic_vision import AnthropicVisionProvider
from app.ai.registry import FEATURES, default_model
from app.core.config import settings
from app.services.book_identifier import ScanUnavailableError
from perf import fake_provider, perf_setup, run_scan_abuse

IMAGE = b"\xff\xd8\xff-fake-jpeg"


@pytest.fixture
def stub():
    server, state = fake_provider.make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base, state
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def provider():
    spec = dataclasses.replace(
        FEATURES["scan.standard"],
        provider="anthropic",
        model=default_model("scan.standard", "anthropic"),
    )
    return AnthropicVisionProvider(spec)


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read())


def _post(url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body or {}).encode(), method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read())


async def test_the_real_adapter_parses_the_fake_reply(stub, provider):
    base, state = stub
    with (
        patch.object(settings, "anthropic_base_url", base),
        patch.object(settings, "anthropic_api_key", "perf-fake-key"),
    ):
        result = await provider.identify(IMAGE)

    assert result.outcome == "ok"
    assert [c.title for c in result.candidates] == ["Dune", "The Great Gatsby"]
    assert result.input_tokens == fake_provider.DEFAULT_INPUT_TOKENS
    assert result.output_tokens == fake_provider.DEFAULT_OUTPUT_TOKENS
    assert state.calls == 1 and state.errors == 0


async def test_injected_error_is_counted_and_refunds_the_scan(stub, provider):
    base, state = stub
    state.error_rate = 1.0
    with (
        patch.object(settings, "anthropic_base_url", base),
        patch.object(settings, "anthropic_api_key", "perf-fake-key"),
        pytest.raises(ScanUnavailableError),
    ):
        await provider.identify(IMAGE)
    # A 4xx is not retried: one injected error is one provider request.
    assert state.calls == 1 and state.errors == 1


def test_count_reset_and_config_endpoints(stub):
    base, _state = stub
    _post(base + "/v1/messages", {"x": 1})
    snapshot = _get(base + "/__count")
    assert snapshot["calls"] == 1 and snapshot["max_in_flight"] == 1

    config = _post(base + "/__config", {"latency_ms": 5, "error_rate": 7})
    assert config["latency_ms"] == 5 and config["error_rate"] == 1.0  # clamped

    assert _post(base + "/__reset")["calls"] == 0
    assert _get(base + "/__count")["max_in_flight"] == 0


def test_bad_config_and_unknown_paths(stub):
    base, _state = stub
    with pytest.raises(urllib.error.HTTPError) as bad:
        _post(base + "/__config", {"latency_ms": "slow"})
    assert bad.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as missing:
        _get(base + "/nope")
    assert missing.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as missing_post:
        _post(base + "/nope")
    assert missing_post.value.code == 404


def test_open_library_answers_with_no_documents(stub):
    base, _state = stub
    assert _get(base + "/search.json?title=x") == {"docs": []}


class TestIsolatedDatabaseGuard:
    def test_remote_database_is_refused(self, monkeypatch):
        monkeypatch.setenv("PERF_CONFIRM_ISOLATED_DB", "yes")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.example.com/x")
        with pytest.raises(perf_setup.UnsafeDatabaseError):
            perf_setup.require_isolated_db()

    def test_local_database_needs_the_confirmation(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/x")
        monkeypatch.delenv("PERF_CONFIRM_ISOLATED_DB", raising=False)
        with pytest.raises(perf_setup.UnsafeDatabaseError):
            perf_setup.require_isolated_db()

    def test_confirmed_local_database_is_allowed(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1/x")
        monkeypatch.setenv("PERF_CONFIRM_ISOLATED_DB", "yes")
        perf_setup.require_isolated_db()


def test_throwaway_jwt_keys_are_a_matching_rs256_pair():
    import jwt

    private, public = perf_setup.generate_jwt_keys()
    token = jwt.encode({"sub": "x"}, private, algorithm="RS256")
    assert jwt.decode(token, public, algorithms=["RS256"]) == {"sub": "x"}


class TestScenarioPlan:
    def test_breaker_window_is_non_trivial(self):
        plan = run_scan_abuse.build_scenarios(cost_micros=7_000, est_micros=10_000)
        env = plan["breaker"].locust_env
        ceiling, cost, est = (
            env["PERF_CEILING_MICROS"],
            env["PERF_COST_MICROS"],
            env["PERF_EST_MICROS"],
        )
        assert est >= cost  # the upper bound in the locustfile relies on it
        assert ceiling // est < (ceiling - est) // cost + 1  # a real window
        assert plan["breaker"].settings["breaker.free_daily_ceiling_micros"] == ceiling

    def test_abuse_cap_matches_the_app_setting(self):
        plan = run_scan_abuse.build_scenarios(7_000, 10_000)["abuse"]
        assert plan.settings["quota.free.found_daily"] == plan.locust_env["PERF_CAP"]
        assert plan.tokens == 1 and plan.users > 1

    def test_tamper_changes_only_what_is_asserted(self):
        plan = run_scan_abuse.build_scenarios(7_000, 10_000)
        for scenario in plan.values():
            before_settings = dict(scenario.settings)
            before_env = dict(scenario.locust_env)
            run_scan_abuse.tamper(scenario)
            assert scenario.settings == before_settings
            assert scenario.locust_env != before_env

    def test_the_fake_reply_costs_no_more_than_the_estimate(self):
        from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros

        cost = cost_micros(
            default_model("scan.standard", "anthropic"),
            CURRENT_PRICE_VERSION,
            input_tokens=fake_provider.DEFAULT_INPUT_TOKENS,
            output_tokens=fake_provider.DEFAULT_OUTPUT_TOKENS,
        )
        assert 0 < cost <= FEATURES["scan.standard"].est_cost_ceiling_micros
