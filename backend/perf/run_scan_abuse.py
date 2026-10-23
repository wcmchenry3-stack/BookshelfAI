"""Run the /scan load scenarios end to end (#539).

    cd backend
    PERF_CONFIRM_ISOLATED_DB=yes DATABASE_URL=postgresql+asyncpg://test:test@localhost/perf \\
        python perf/run_scan_abuse.py [--scenario abuse|breaker|kill|all] [--tamper]

Prerequisites: the schema is migrated (``alembic upgrade head``) in a THROWAWAY
database (the run deletes AI quota, spend, usage, scan-record and settings
rows), and ``pip install -r requirements-perf.txt``.

For each scenario this script: clears the database and creates Free users with
tokens signed by a throwaway RS256 key pair; writes the scenario's
``ai_settings`` (``scan.standard`` is switched to the Anthropic adapter, whose
base URL points at the fake provider); starts the API with uvicorn; runs
Locust headless with ``locustfile_scan.py`` (which asserts when it quits); and
stops the API. The fake provider (``fake_provider.py``) is started here unless
``--external-stub`` says the caller (the workflow) already runs it at
``--stub-port``. Exit code 0 only when every scenario passed.

Settings of the app under test (only in the processes started here):

- ``RATE_LIMIT_SCAN=100000/minute``: the production 10/minute per user would
  throttle one Locust process before any quota is reached, and then the run
  would measure the rate limit instead of the quota. The default in
  ``app/core/config.py`` is unchanged.
- ``AI_SETTINGS_CACHE_TTL_SECONDS=2``: the kill switch is read through a cache
  (30 s by default); a short TTL keeps the run short. The assertion uses the
  configured value.
- ``TURNSTILE_REQUIRED=false``, ``CLAMAV_ENABLED=false``: no Turnstile secret
  and no clamd in this stack. Image normalisation still runs.
- ``HTTPS_PROXY`` points at a dead local port, so nothing reaches the public
  internet (Google Books) from enrichment; Open Library is the fake.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

TTL_S = 2
HUGE = 1_000_000_000
STANDARD_FEATURE = "scan.standard"


@dataclass
class Scenario:
    name: str
    users: int  # Locust users (connections)
    tokens: int  # distinct app users
    run_time_s: int
    latency_ms: int
    settings: dict
    locust_env: dict = field(default_factory=dict)


def build_scenarios(cost_micros: int, est_micros: int) -> dict[str, Scenario]:
    open_dials = {
        "quota.free.found_daily": 1000,
        "scan.requests_per_user_daily": 10_000,
        "breaker.free_daily_ceiling_micros": HUGE,
    }
    cap = 3
    ceiling = 6 * est_micros
    return {
        "abuse": Scenario(
            "abuse",
            users=4,
            tokens=1,
            run_time_s=15,
            latency_ms=300,
            settings={**open_dials, "quota.free.found_daily": cap},
            locust_env={"PERF_CAP": cap},
        ),
        "breaker": Scenario(
            "breaker",
            users=8,
            tokens=8,
            run_time_s=20,
            latency_ms=200,
            settings={
                **open_dials,
                "breaker.free_daily_ceiling_micros": ceiling,
            },
            locust_env={
                "PERF_CEILING_MICROS": ceiling,
                "PERF_COST_MICROS": cost_micros,
                "PERF_EST_MICROS": est_micros,
                "PERF_USERS": 8,
            },
        ),
        "kill": Scenario(
            "kill",
            users=4,
            tokens=4,
            run_time_s=25,
            latency_ms=100,
            settings=open_dials,
            locust_env={
                "PERF_FLIP_AT_S": 8,
                "PERF_TTL_S": TTL_S,
                "PERF_MARGIN_S": 1.0,
            },
        ),
    }


def tamper(scenario: Scenario) -> None:
    """Make what Locust ASSERTS wrong while the app keeps its real settings,
    to show the assertions fail the run."""
    env = scenario.locust_env
    if scenario.name == "abuse":
        env["PERF_CAP"] = env["PERF_CAP"] - 1
    elif scenario.name == "breaker":
        env["PERF_CEILING_MICROS"] = env["PERF_CEILING_MICROS"] // 2
    elif scenario.name == "kill":
        env["PERF_TTL_S"] = 0.2
        env["PERF_MARGIN_S"] = 0.0


def wait_for(url: str, timeout_s: float, what: str) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    raise RuntimeError(f"{what} did not come up at {url}")


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_scenario(
    scenario: Scenario, *, stub_url: str, api_port: int, app_env: dict, log_dir: Path
) -> bool:
    from perf import perf_setup

    print(f"\n=== scenario: {scenario.name} ===", flush=True)
    tokens = asyncio.run(perf_setup.prepare(scenario.tokens, scenario.settings))
    urllib.request.urlopen(
        urllib.request.Request(
            stub_url + "/__config",
            data=json.dumps({"latency_ms": scenario.latency_ms}).encode(),
            method="POST",
        )
    ).read()

    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / f"api-{scenario.name}.log", "wb") as api_log:
        api = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
                "--no-proxy-headers",
                "--log-level",
                "warning",
            ],
            cwd=BACKEND_DIR,
            env=app_env,
            stdout=api_log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for(f"http://127.0.0.1:{api_port}/health", 60, "the API")
            with tempfile.TemporaryDirectory() as tmp:
                token_file = Path(tmp) / "tokens.json"
                token_file.write_text(json.dumps(tokens))
                env = {
                    **app_env,
                    "PERF_SCENARIO": scenario.name,
                    "PERF_STUB_URL": stub_url,
                    "PERF_TOKENS_FILE": str(token_file),
                    "PERF_LATENCY_MS": str(scenario.latency_ms),
                    **{k: str(v) for k, v in scenario.locust_env.items()},
                }
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "locust",
                        "-f",
                        str(BACKEND_DIR / "perf" / "locustfile_scan.py"),
                        "--headless",
                        "--users",
                        str(scenario.users),
                        "--spawn-rate",
                        str(scenario.users),
                        "--run-time",
                        f"{scenario.run_time_s}s",
                        "--host",
                        f"http://127.0.0.1:{api_port}",
                        "--only-summary",
                    ],
                    cwd=BACKEND_DIR,
                    env=env,
                    check=False,
                )
        finally:
            stop(api)
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--scenario", choices=["abuse", "breaker", "kill", "all"], default="all"
    )
    parser.add_argument("--stub-port", type=int, default=18080)
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument(
        "--external-stub",
        action="store_true",
        help="the fake provider is already running at --stub-port",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=BACKEND_DIR / "perf-logs",
        help="where the API's log of each scenario is written",
    )
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="assert a wrong cap / ceiling / TTL: the run must FAIL (proves "
        "the assertions can fail)",
    )
    args = parser.parse_args(argv)

    from perf import perf_setup

    perf_setup.require_isolated_db()
    stub_url = f"http://127.0.0.1:{args.stub_port}"
    private, public = perf_setup.generate_jwt_keys()
    app_env = {
        **os.environ,
        "ENVIRONMENT": "development",
        "JWT_PRIVATE_KEY": private,
        "JWT_PUBLIC_KEY": public,
        "TURNSTILE_REQUIRED": "false",
        "TURNSTILE_SECRET_KEY": "",
        "CLAMAV_ENABLED": "false",
        "ANTHROPIC_API_KEY": "perf-fake-key",
        "AI_ANTHROPIC_BASE_URL": stub_url,
        "OPENAI_API_KEY": "",
        "OPEN_LIBRARY_BASE_URL": stub_url,
        "RATE_LIMIT_SCAN": "100000/minute",
        "AI_SETTINGS_CACHE_TTL_SECONDS": str(TTL_S),
        "SENTRY_DSN": "",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "https_proxy": "http://127.0.0.1:9",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        app_env.pop(name, None)
    os.environ.update(
        {
            k: app_env[k]
            for k in (
                "ENVIRONMENT",
                "JWT_PRIVATE_KEY",
                "JWT_PUBLIC_KEY",
                "TURNSTILE_REQUIRED",
                "AI_ANTHROPIC_BASE_URL",
                "RATE_LIMIT_SCAN",
                "AI_SETTINGS_CACHE_TTL_SECONDS",
            )
        }
    )

    # The cost of one fake reply and the breaker's estimate, from the app's
    # own price table and registry (imported after the environment is set).
    from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
    from app.ai.registry import default_model, get_feature
    from perf import fake_provider

    est = get_feature(STANDARD_FEATURE).est_cost_ceiling_micros
    cost = cost_micros(
        default_model(STANDARD_FEATURE, "anthropic"),
        CURRENT_PRICE_VERSION,
        input_tokens=fake_provider.DEFAULT_INPUT_TOKENS,
        output_tokens=fake_provider.DEFAULT_OUTPUT_TOKENS,
    )
    if cost > est:
        raise SystemExit(
            f"the fake reply costs {cost} micros, above the {est} estimate; "
            "lower its token counts"
        )

    scenarios = build_scenarios(cost, est)
    names = list(scenarios) if args.scenario == "all" else [args.scenario]

    stub = None
    if not args.external_stub:
        stub = subprocess.Popen(
            [
                sys.executable,
                str(BACKEND_DIR / "perf" / "fake_provider.py"),
                "--port",
                str(args.stub_port),
            ]
        )
    try:
        wait_for(stub_url + "/__count", 15, "the fake provider")
        outcomes = {}
        for name in names:
            scenario = scenarios[name]
            if args.tamper:
                tamper(scenario)
            outcomes[name] = run_scenario(
                scenario,
                stub_url=stub_url,
                api_port=args.api_port,
                app_env=app_env,
                log_dir=args.log_dir,
            )
    finally:
        if stub is not None:
            stop(stub)

    print("\n=== summary ===")
    for name, passed in outcomes.items():
        print(f"{name}: {'passed' if passed else 'FAILED'}")
    failed = [n for n, ok in outcomes.items() if not ok]
    if args.tamper:
        # Inverted: every tampered scenario must have failed.
        if len(failed) == len(outcomes):
            print("tamper check: every scenario failed, as it must")
            return 0
        print("tamper check: a scenario passed with a wrong expectation")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
