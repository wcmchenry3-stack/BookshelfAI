"""Load scenarios for POST /scan against a fake provider (#539).

Proves, under concurrent load, that the quota, the in-flight lock, the cost
breaker and the kill switch hold. Not a performance benchmark: nothing here
asserts on latency or throughput.

Pick one scenario per run with ``PERF_SCENARIO`` (the runner,
``run_scan_abuse.py``, does that and prepares the database, the app and the
fake provider; read it first):

``abuse``    (a) several connections of ONE user hammer /scan. The provider is
             reached at most ``quota.free.found_daily`` times, never twice at
             once for that user (the in-flight lock), and the rest are refused
             with 429 ``daily_scan_limit`` / ``scan_in_progress``.
``breaker``  (b) many users drive the daily cost breaker. Once the ceiling is
             reached /scan answers 503 ``ai_budget_reached`` and the provider
             stops being called; the number of provider calls matches the cost
             of a call against ``breaker.free_daily_ceiling_micros``.
``kill``     (c) ``scan.standard.enabled`` is set to false mid-run, with the
             operator CLI (``python -m app.ai.settings set``). Within the
             settings cache TTL /scan answers 503 ``scan_disabled`` and the
             provider stops being called.

Every photo is different (random pixel noise), so scan records (#585) never
answer a request: the run measures the quota, not the cache.

The assertions run when Locust quits and set a non-zero exit code when one
fails. 429s and the documented 503 refusals are expected responses; any other
status (a 500 above all) is a failure.
"""

from __future__ import annotations

import io
import itertools
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import gevent
from locust import HttpUser, between, events, task
from PIL import Image

SCENARIO = os.environ.get("PERF_SCENARIO", "")
STUB_URL = os.environ.get("PERF_STUB_URL", "http://127.0.0.1:18080")
BACKEND_DIR = Path(__file__).resolve().parent.parent

# What each scenario may see, besides 200: (status, detail).
EXPECTED_REFUSALS = {
    "abuse": {
        (429, "daily_scan_limit"),
        (429, "scan_in_progress"),
    },
    "breaker": {
        (503, "ai_budget_reached"),
        (429, "scan_in_progress"),
    },
    "kill": {
        (503, "scan_disabled"),
        (429, "scan_in_progress"),
    },
}


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


TOKENS: list[str] = []
if os.environ.get("PERF_TOKENS_FILE"):
    TOKENS = json.loads(Path(os.environ["PERF_TOKENS_FILE"]).read_text())

# One entry per /scan response: (started_at, status, detail).
RESULTS: list[tuple[float, int, str]] = []
# (time, provider calls) every half second, from the fake's /__count.
SAMPLES: list[tuple[float, int]] = []
STATE: dict = {
    "flip_started_at": None,
    "flipped_at": None,
    "calls_before_flip": None,
    "sample_errors": 0,
    "sample_attempts": 0,
}
# Exit code of a run whose assertions failed (Locust's own failure exit is 1),
# so the runner can tell "an assertion failed" from "something crashed".
ASSERTION_EXIT_CODE = 3
# Fewest samples that make a "calls stopped" check meaningful.
MIN_SAMPLES = 4


def stub_get(path: str) -> dict:
    with urllib.request.urlopen(STUB_URL + path, timeout=5) as resp:
        return json.loads(resp.read())


def stub_post(path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(
        STUB_URL + path, data=json.dumps(body or {}).encode(), method="POST"
    )
    with urllib.request.urlopen(request, timeout=5) as resp:
        return json.loads(resp.read())


def noise_jpeg() -> bytes:
    """A small valid JPEG of random pixels: different bytes every time, so the
    normalised photo (and its scan-record fingerprint) is new."""
    image = Image.frombytes("RGB", (64, 64), os.urandom(64 * 64 * 3))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _detail(response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        return ""
    return detail if isinstance(detail, str) else ""


class ScanUser(HttpUser):
    abstract = True
    wait_time = between(0.0, 0.05)
    _next_index = itertools.count()

    def on_start(self) -> None:
        self.token = self.pick_token(next(ScanUser._next_index))

    def pick_token(self, index: int) -> str:
        return TOKENS[index % len(TOKENS)]

    @task
    def scan(self) -> None:
        started = time.time()
        with self.client.post(
            "/scan",
            headers={"Authorization": f"Bearer {self.token}"},
            files={"file": ("photo.jpg", noise_jpeg(), "image/jpeg")},
            catch_response=True,
            name="POST /scan",
        ) as response:
            detail = "" if response.status_code == 200 else _detail(response)
            RESULTS.append((started, response.status_code, detail))
            if response.status_code == 200 or (
                (response.status_code, detail) in EXPECTED_REFUSALS[SCENARIO]
            ):
                response.success()
            else:
                response.failure(f"unexpected {response.status_code} {detail!r}")


if SCENARIO == "abuse":

    class AbusiveUser(ScanUser):
        """Every connection is the same user."""

        def pick_token(self, index: int) -> str:
            return TOKENS[0]

elif SCENARIO in {"breaker", "kill"}:

    class DistinctUser(ScanUser):
        """One user per connection."""


# ---------------------------------------------------------------------------
# Sampling and the mid-run kill switch
# ---------------------------------------------------------------------------


def _sampler() -> None:
    while True:
        STATE["sample_attempts"] += 1
        try:
            SAMPLES.append((time.time(), stub_get("/__count")["calls"]))
        except (OSError, ValueError, KeyError):
            STATE["sample_errors"] += 1
        gevent.sleep(0.5)


def _flip_kill_switch() -> None:
    gevent.sleep(_env_float("PERF_FLIP_AT_S", 10))
    try:
        STATE["calls_before_flip"] = stub_get("/__count")["calls"]
    except (OSError, ValueError, KeyError):
        pass
    # Before the CLI starts: the earliest the switch can take effect.
    STATE["flip_started_at"] = time.time()
    # The operator's own tool, so the real validation and write path is used.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "app.ai.settings",
            "set",
            "scan.standard.enabled",
            "false",
            "--by",
            "perf",
        ],
        cwd=BACKEND_DIR,
        check=True,
        capture_output=True,
    )
    # After the commit: the switch has taken effect by here at the latest.
    STATE["flipped_at"] = time.time()


@events.test_start.add_listener
def _on_start(environment, **_kwargs) -> None:
    if not SCENARIO:
        raise SystemExit("PERF_SCENARIO must be abuse, breaker or kill")
    if not TOKENS:
        raise SystemExit("PERF_TOKENS_FILE is empty or unset")
    stub_post("/__reset")
    stub_post("/__config", {"latency_ms": _env_float("PERF_LATENCY_MS", 0)})
    gevent.spawn(_sampler)
    if SCENARIO == "kill":
        gevent.spawn(_flip_kill_switch)


# ---------------------------------------------------------------------------
# Post-run assertions
# ---------------------------------------------------------------------------


class Report:
    """Assertions with ids. ``PERF_RESULT_FILE`` gets them as JSON, so the
    runner can check that a deliberately wrong expectation failed for the
    right reason."""

    def __init__(self) -> None:
        self.results: dict[str, bool] = {}
        self.failures: list[str] = []

    def check(self, key: str, ok: bool, text: str) -> None:
        print(f"PERF {'PASS' if ok else 'FAIL'}: {text}", flush=True)
        self.results[key] = ok
        if not ok:
            self.failures.append(text)

    def write(self) -> None:
        path = os.environ.get("PERF_RESULT_FILE")
        if path:
            Path(path).write_text(json.dumps(self.results))


def _count(status: int, detail: str | None = None) -> int:
    return sum(
        1 for _t, s, d in RESULTS if s == status and (detail is None or d == detail)
    )


def _common(report: Report, final: dict) -> None:
    histogram: dict[str, int] = {}
    for _t, status, detail in RESULTS:
        key = f"{status} {detail}".strip()
        histogram[key] = histogram.get(key, 0) + 1
    print(f"PERF responses: {json.dumps(histogram, sort_keys=True)}", flush=True)
    print(f"PERF provider stub: {json.dumps(final, sort_keys=True)}", flush=True)
    report.check(
        "responses", len(RESULTS) > 0, f"{len(RESULTS)} /scan responses recorded"
    )
    report.check("no_500", _count(500) == 0, "no 500 responses")
    allowed = EXPECTED_REFUSALS[SCENARIO]
    unexpected = {
        f"{s} {d}".strip() for _t, s, d in RESULTS if s != 200 and (s, d) not in allowed
    }
    report.check(
        "expected_only", not unexpected, f"no unexpected responses {sorted(unexpected)}"
    )
    attempts, errors = STATE["sample_attempts"], STATE["sample_errors"]
    report.check(
        "sampling",
        attempts > 0 and errors * 4 <= attempts,
        f"provider-count sampling worked: {attempts - errors} of {attempts} samples "
        f"({errors} errors)",
    )


def _assert_abuse(report: Report, final: dict) -> None:
    cap = _env_int("PERF_CAP", 3)
    ok = _count(200)
    report.check(
        "ok_ge_1", ok >= 1, f"{ok} scans succeeded (the run reached the provider)"
    )
    report.check("ok_le_cap", ok <= cap, f"{ok} successful scans <= daily limit {cap}")
    report.check(
        "calls_ge_1",
        final["calls"] >= 1,
        f"the provider was called ({final['calls']} calls)",
    )
    report.check(
        "calls_le_cap",
        final["calls"] <= cap + final["errors"],
        f"provider calls {final['calls']} <= daily limit {cap} "
        f"(+ {final['errors']} injected errors)",
    )
    # Every photo is new, so every 200 is exactly one provider call.
    report.check(
        "calls_eq_ok",
        final["calls"] - final["errors"] == ok,
        f"provider calls {final['calls']} - {final['errors']} errors == "
        f"{ok} successful scans",
    )
    report.check(
        "limit_hit",
        _count(429, "daily_scan_limit") >= 1,
        "the limit was hit: 429 daily_scan_limit seen",
    )
    report.check(
        "in_progress",
        _count(429, "scan_in_progress") >= 1,
        "parallel requests were refused: 429 scan_in_progress seen",
    )
    report.check(
        "one_at_a_time",
        1 <= final["max_in_flight"] <= 1,
        f"exactly 1 provider call at once for one user (saw {final['max_in_flight']})",
    )


def _tail_stable(end: float, tail_s: float) -> tuple[list[int], bool]:
    tail = [c for t, c in SAMPLES if t >= end - tail_s]
    return tail, len(tail) >= MIN_SAMPLES and len(set(tail)) == 1


def _assert_breaker(report: Report, final: dict) -> None:
    ceiling = _env_int("PERF_CEILING_MICROS", 60_000)
    cost = _env_int("PERF_COST_MICROS", 7_000)
    est = _env_int("PERF_EST_MICROS", 10_000)
    calls = final["calls"]
    # A Free call is admitted only while reserved + settled + est <= ceiling,
    # and est >= cost, so at most (ceiling - est) // cost + 1 calls are ever
    # admitted; a refusal needs u * est + s * cost + est > ceiling with
    # u + s calls admitted, so at least ceiling // est were.
    high = (ceiling - est) // cost + 1
    low = ceiling // est
    report.check(
        "opened",
        _count(503, "ai_budget_reached") >= 1,
        "the breaker opened: 503 ai_budget_reached seen",
    )
    report.check(
        "calls_window",
        low <= calls <= high and calls >= 1,
        f"provider calls {calls} within [{low}, {high}] for ceiling {ceiling}, "
        f"call cost {cost}, estimate {est} micros",
    )
    tail_s = _env_float("PERF_TAIL_S", 3)
    end = SAMPLES[-1][0] if SAMPLES else 0.0
    tail, stable = _tail_stable(end, tail_s)
    refused_in_tail = sum(
        1
        for t, s, d in RESULTS
        if t >= end - tail_s and (s, d) == (503, "ai_budget_reached")
    )
    report.check(
        "stopped",
        stable and refused_in_tail >= 1 and tail[-1] == calls,
        f"provider calls stopped: {len(tail)} samples over the last {tail_s:g}s "
        f"all equal {calls} while {refused_in_tail} requests were refused",
    )


def _assert_kill(report: Report, final: dict) -> None:
    ttl = _env_float("PERF_TTL_S", 2)
    margin = _env_float("PERF_MARGIN_S", 2.0)
    latency_s = _env_float("PERF_LATENCY_MS", 0) / 1000
    started, flipped = STATE["flip_started_at"], STATE["flipped_at"]
    report.check("flipped", flipped is not None, "the kill switch was flipped mid-run")
    if flipped is None or started is None:
        return
    # Late requests: measured from AFTER the CLI returned (the commit is done
    # by then). Measuring from before would blame requests that legitimately
    # still hit a cache filled just before the commit.
    cutoff = flipped + ttl + margin
    before = [r for r in RESULTS if r[0] < started]
    after = [r for r in RESULTS if r[0] >= cutoff]
    report.check(
        "ok_before",
        any(s == 200 for _t, s, _d in before),
        f"{sum(1 for r in before if r[1] == 200)} scans succeeded before the switch",
    )
    report.check(
        "calls_before",
        (STATE["calls_before_flip"] or 0) >= 1,
        f"the provider was called {STATE['calls_before_flip']} times before the switch",
    )
    report.check(
        "after",
        len(after) >= 1,
        f"{len(after)} requests were made after the cache TTL ({ttl:g}s + {margin:g}s)",
    )
    late = [r for r in after if (r[1], r[2]) != (503, "scan_disabled")]
    report.check(
        "late_requests",
        not late,
        f"every request started {ttl:g}s + {margin:g}s after the switch got "
        f"503 scan_disabled ({len(late)} did not)",
    )
    # First refusal: measured from BEFORE the CLI started, the strictest
    # reading (the switch took effect somewhere after it); the margin has to
    # cover the CLI's own start-up.
    disabled = [t for t, s, d in RESULTS if (s, d) == (503, "scan_disabled")]
    first = min(disabled) - started if disabled else None
    report.check(
        "first_refusal",
        first is not None and first <= ttl + margin,
        f"first 503 scan_disabled at {first if first is None else round(first, 2)}s "
        f"after the CLI started (limit {ttl + margin:g}s)",
    )
    post = [c for t, c in SAMPLES if t >= flipped + ttl + latency_s]
    report.check(
        "stopped",
        len(post) >= MIN_SAMPLES and set(post) == {final["calls"]},
        f"provider calls stopped: {len(post)} samples after the TTL"
        f" ({sorted(set(post))}), final read {final['calls']}",
    )


@events.quitting.add_listener
def _on_quitting(environment, **_kwargs) -> None:
    # Let calls that were already at the provider when the run stopped finish.
    gevent.sleep(_env_float("PERF_LATENCY_MS", 0) / 1000 + 0.5)
    report = Report()
    try:
        final = stub_get("/__count")
    except OSError as exc:
        print(f"PERF FAIL: the fake provider is not reachable ({exc})", flush=True)
        environment.process_exit_code = ASSERTION_EXIT_CODE
        return
    _common(report, final)
    {"abuse": _assert_abuse, "breaker": _assert_breaker, "kill": _assert_kill}[
        SCENARIO
    ](report, final)
    print(
        f"PERF {SCENARIO}: "
        + ("all assertions passed" if not report.failures else "FAILED"),
        flush=True,
    )
    report.write()
    if report.failures:
        environment.process_exit_code = ASSERTION_EXIT_CODE
