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
from typing import ClassVar
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
        patch.object(settings, "ai_anthropic_base_url", base),
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
        patch.object(settings, "ai_anthropic_base_url", base),
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

    reset = _post(base + "/__reset")
    # Counters AND the start-up latency / error rate come back.
    assert reset["calls"] == 0 and reset["latency_ms"] == 0 and reset["error_rate"] == 0
    assert _get(base + "/__count")["max_in_flight"] == 0


def test_count_reports_the_tokens_every_reply_carries(stub):
    base, _state = stub
    counted = _get(base + "/__count")
    assert counted["input_tokens"] == fake_provider.DEFAULT_INPUT_TOKENS
    assert counted["output_tokens"] == fake_provider.DEFAULT_OUTPUT_TOKENS


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
    OK = "postgresql+asyncpg://test:test@localhost:5432/bs539_perf"

    def test_confirmed_local_perf_database_is_allowed(self):
        perf_setup.check_database_url(self.OK, "yes")
        perf_setup.check_database_url("postgresql+asyncpg://u:p@127.0.0.1/perf", "yes")
        perf_setup.check_database_url("postgresql+asyncpg://u:p@[::1]/perf", "yes")

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+asyncpg://u:p@db.example.com/perf",
            "postgresql+asyncpg://u:p@localhost.evil.example.com/perf",
            "postgresql+asyncpg://u:p@localhost,db.example.com/perf",
            "postgresql+asyncpg:///perf",  # no host: the default socket
            "postgresql+asyncpg://u:p@localhost/perf?host=db.example.com",
            "postgresql+asyncpg://u:p@localhost/perf?HOSTADDR=10.0.0.5",
            "postgresql+asyncpg://u:p@localhost/perf?service=prod",
            "postgresql+asyncpg://u:p@localhost/perf?database=bookshelf",
            "postgresql+asyncpg://u:p@localhost/perf?Database=x",
            "postgresql+asyncpg://u:p@localhost/perf?dbname=bookshelf",
            "postgresql+asyncpg://u:p@localhost/perf?user=admin",
            "postgresql+asyncpg://u:p@localhost/perf?sslmode=disable",
            "postgresql+asyncpg://u:p@localhost/bookshelf",
            "postgresql+asyncpg://u:p@localhost/",
            "not a url",
            "",
        ],
    )
    def test_unsafe_url_is_refused(self, url):
        with pytest.raises(perf_setup.UnsafeDatabaseError) as exc:
            perf_setup.check_database_url(url, "yes")
        assert "u:p" not in str(exc.value)

    @pytest.mark.parametrize("database", ["bs539_perf", "perf", "PERF_x"])
    def test_connected_database_accepted(self, database):
        perf_setup.check_server_identity(database)

    @pytest.mark.parametrize("database", ["bookshelf", "", None])
    def test_connected_database_refused(self, database):
        with pytest.raises(perf_setup.UnsafeDatabaseError):
            perf_setup.check_server_identity(database)

    @pytest.mark.parametrize("confirmed", [None, "", "no", "YES", "true"])
    def test_needs_the_confirmation(self, confirmed):
        with pytest.raises(perf_setup.UnsafeDatabaseError):
            perf_setup.check_database_url(self.OK, confirmed)

    def test_require_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", self.OK)
        monkeypatch.delenv("PERF_CONFIRM_ISOLATED_DB", raising=False)
        with pytest.raises(perf_setup.UnsafeDatabaseError):
            perf_setup.require_isolated_db()
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


class TestTamperProof:
    HEALTHY: ClassVar[dict] = {
        "responses": True,
        "no_500": True,
        "expected_only": True,
        "sampling": True,
    }

    def test_target_assertion_failing_with_a_healthy_run_is_proof(self):
        results = {**self.HEALTHY, "ok_le_cap": False, "calls_le_cap": False}
        assert run_scan_abuse.tamper_proven("abuse", 3, results)

    @pytest.mark.parametrize("code", [0, 1, 2, -9])
    def test_other_exit_codes_are_not_proof(self, code):
        results = {**self.HEALTHY, "calls_window": False}
        assert not run_scan_abuse.tamper_proven("breaker", code, results)

    def test_a_crashed_run_is_not_proof(self):
        assert not run_scan_abuse.tamper_proven("kill", 3, None)
        assert not run_scan_abuse.tamper_proven("kill", 3, {})
        unhealthy = {**self.HEALTHY, "responses": False, "late_requests": False}
        assert not run_scan_abuse.tamper_proven("kill", 3, unhealthy)

    def test_failing_something_else_is_not_proof(self):
        results = {
            **self.HEALTHY,
            "stopped": False,
            "late_requests": True,
            "first_refusal": True,
        }
        assert not run_scan_abuse.tamper_proven("kill", 3, results)

    def test_kill_tamper_puts_the_cutoff_before_the_switch(self):
        scenario = run_scan_abuse.build_scenarios(7_000, 10_000)["kill"]
        run_scan_abuse.tamper(scenario)
        env = scenario.locust_env
        assert env["PERF_TTL_S"] + env["PERF_MARGIN_S"] < 0


def test_port_in_use_sees_a_running_stub(stub):
    base, _state = stub
    port = int(base.rsplit(":", 1)[1])
    assert run_scan_abuse.port_in_use(port)


@pytest.mark.integration
class TestServerIdentityOnRealPostgres:
    """The post-connect check against the real server: the database it
    reports must be a perf one before anything is deleted."""

    @staticmethod
    def _url(name: str) -> str:
        import os

        from sqlalchemy.engine import make_url

        return (
            make_url(os.environ["DATABASE_URL"])
            .set(drivername="postgresql+asyncpg")
            .set(database=name)
            .render_as_string(hide_password=False)
        )

    async def _verify(self, name: str):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(self._url(name))
        try:
            async with engine.connect() as conn:
                await perf_setup.verify_connected_database(conn)
        finally:
            await engine.dispose()

    async def test_non_perf_database_is_refused_and_perf_is_accepted(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        admin = create_async_engine(self._url("postgres"), isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.execute(text("DROP DATABASE IF EXISTS guardcheck_tmp"))
                await conn.execute(text("DROP DATABASE IF EXISTS guardcheck_perf_tmp"))
                await conn.execute(text("CREATE DATABASE guardcheck_tmp"))
                await conn.execute(text("CREATE DATABASE guardcheck_perf_tmp"))
            try:
                with pytest.raises(perf_setup.UnsafeDatabaseError):
                    await self._verify("guardcheck_tmp")
                await self._verify("guardcheck_perf_tmp")
            finally:
                async with admin.connect() as conn:
                    await conn.execute(text("DROP DATABASE IF EXISTS guardcheck_tmp"))
                    await conn.execute(
                        text("DROP DATABASE IF EXISTS guardcheck_perf_tmp")
                    )
        finally:
            await admin.dispose()


async def test_reset_and_create_users_mints_tokens_with_tv_claim():
    """The nightly harness mints one access token per Free user with the
    app's own create_access_token; the tv claim must match the new row
    (token_version 0), or every scan request would be 401 token_revoked."""
    from unittest.mock import AsyncMock, MagicMock

    import jwt as pyjwt

    from app.core import database
    from app.models import User

    private, public = perf_setup.generate_jwt_keys()
    added = []
    session = MagicMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()
    session.add = added.append

    class _Session:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    with (
        patch.object(settings, "jwt_private_key", private),
        patch.object(settings, "jwt_public_key", public),
        patch.object(perf_setup, "require_isolated_db"),
        patch.object(perf_setup, "verify_connected_database", AsyncMock()),
        patch.object(database, "AsyncSessionLocal", _Session),
    ):
        tokens = await perf_setup._reset_and_create_users(2)

    assert len(tokens) == 2
    users = [u for u in added if isinstance(u, User)]
    assert len(users) == 2
    for token, user in zip(tokens, users, strict=True):
        claims = pyjwt.decode(token, public, algorithms=["RS256"])
        assert claims["sub"] == str(user.id)
        assert claims["tv"] == user.token_version == 0
    session.commit.assert_awaited_once()
