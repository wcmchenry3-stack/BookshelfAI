"""Scan evaluation runner (#536): spend cap, refusals, report metrics.

The provider is faked at the adapter boundary (the gateway's adapter
classes, or an injected factory); R2 is an in-memory fake. No network."""

import json
import math
from dataclasses import replace
from unittest.mock import patch

import pytest

from app.ai import gateway
from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
from app.ai.providers.base import ProviderTimeoutError
from app.ai.registry import FEATURES
from app.core.config import settings
from app.services.book_identifier import ScanUnavailableError
from eval.ai_scan import EvalError, runner
from eval.ai_scan import run_eval as cli
from eval.ai_scan.dataset import REQUIRED_ENV
from tests import _ai_eval_fakes as f

restore_logger_levels = f.restore_logger_levels
pytestmark = pytest.mark.usefixtures("restore_logger_levels")

HAIKU = runner.resolve_spec("scan.standard", "claude-haiku-5-5")


def _run(script, cases, *, cap_usd=10.0, spec=HAIKU, clock=None, image=None, **kw):
    ds, client = f.dataset(f.objects_for(cases, image=image))
    provider = f.ScriptedProvider(script)
    kwargs = {"provider_factory": lambda _spec: provider, **kw}
    if clock is not None:
        kwargs["clock"] = clock

    async def go():
        return await runner.run_eval(spec, ds, cap_usd=cap_usd, **kwargs)

    import anyio

    return anyio.run(go), provider, client


# --- Spec ---------------------------------------------------------------------------


def test_provider_defaults_from_the_model():
    assert runner.infer_provider("claude-sonnet-5-5") == "anthropic"
    assert runner.infer_provider("gpt-4o-mini") == "openai"
    with pytest.raises(EvalError, match="unknown model"):
        runner.infer_provider("claude-made-up")


def test_spec_keeps_the_registry_limits():
    spec = runner.resolve_spec("scan.enhanced", "claude-sonnet-5-5")
    base = FEATURES["scan.enhanced"]
    assert (spec.provider, spec.model) == ("anthropic", "claude-sonnet-5-5")
    assert spec.max_tokens == base.max_tokens
    assert spec.effort == base.effort
    assert spec.max_image_long_edge_px == base.max_image_long_edge_px


@pytest.mark.parametrize(
    "feature, model, provider",
    [
        ("scan.bogus", "claude-haiku-5-5", None),
        ("scan.standard", "claude-haiku-5-5", "openai"),
    ],
)
def test_invalid_spec_is_refused(feature, model, provider):
    with pytest.raises(EvalError):
        runner.resolve_spec(feature, model, provider)


# --- Spend cap -----------------------------------------------------------------------


def _worst_usage_cost(model, prompt_tokens, output_tokens):
    """The most a call with these token counts can cost: every prompt token
    in its dearest category (input, cache read or cache write)."""
    return max(
        cost_micros(model, CURRENT_PRICE_VERSION, output_tokens=output_tokens, **kw)
        for kw in (
            {"input_tokens": prompt_tokens},
            {"cache_read_tokens": prompt_tokens},
            {"cache_write_tokens": prompt_tokens},
        )
    )


# (model, normalised width, height, image tokens by the published formula)
_PUBLISHED = [
    ("claude-haiku-5-5", 1568, 1568, 3279),
    ("claude-sonnet-5-5", 1568, 1176, 2459),
    ("gpt-4o", 1568, 768, 85 + 170 * 8),
    ("gpt-4o-mini", 1568, 768, 2833 + 5667 * 8),
]


@pytest.mark.parametrize("feature", sorted(FEATURES))
@pytest.mark.parametrize("model, w, h, image", _PUBLISHED)
@pytest.mark.parametrize("max_tokens", [None, 8_192, 16_384])
def test_estimate_bounds_a_worst_case_call(feature, model, w, h, image, max_tokens):
    spec = runner.resolve_spec(feature, model)
    if max_tokens is not None:
        spec = replace(spec, max_tokens=max_tokens)  # a larger SCAN_MAX_TOKENS
    worst = _worst_usage_cost(model, image + 2_000, spec.max_tokens)
    assert runner.estimate_call_micros(spec, w, h) >= worst
    # The bound for unknown dimensions covers the largest image too.
    assert runner.estimate_call_micros(spec) >= worst
    assert runner.estimate_call_micros(spec, w, h) >= spec.est_cost_ceiling_micros


def _png(w, h):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 60, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_cap_is_never_exceeded_on_gpt_4o_mini_at_the_max_long_edge():
    # gpt-4o-mini at high detail: a 1568 x 768 photo is 8 tiles, 48,169
    # image tokens (2,833 + 5,667 x 8). With max_tokens = 8,192 every call
    # really bills (48,169 + 2,000) x $0.15/M + 8,192 x $0.60/M = 12,441
    # micros. A $0.06 cap fits 4 such calls (49,764) but not 5 (62,205).
    spec = replace(
        runner.resolve_spec("scan.standard", "gpt-4o-mini"), max_tokens=8_192
    )
    worst = f.result(
        [("Dune", "Frank Herbert")],
        input_tokens=48_169 + 2_000,
        output_tokens=8_192,
    )
    cases = [f.case(i) for i in range(1, 8)]
    report, provider, _ = _run(
        [worst] * len(cases), cases, cap_usd=0.06, spec=spec, image=_png(1568, 768)
    )
    assert report["cost"]["total_usd"] <= 0.06
    assert report["spend_cap"]["counted_usd"] <= report["spend_cap"]["cap_usd"]
    assert len(provider.images) == 4
    assert report["spend_cap"]["stopped_by_cap"] is True
    assert report["spend_cap"]["largest_call_bound_used_usd"] >= 0.012441


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, True, "10"])
def test_cap_must_be_a_positive_number(bad):
    with pytest.raises(EvalError):
        runner.cap_micros_from_usd(bad)


def _haiku_result(output_tokens):
    return f.result([("Dune", "Frank Herbert")], output_tokens=output_tokens)


def test_spend_cap_stops_before_a_call_that_could_exceed_it():
    # Estimate 1,000 micros; each answered call really costs 600 (1,200
    # Haiku output tokens at $0.50/M). Cap 2,500 micros: calls at 0, 600 and
    # 1,200 spent fit (spent + 1,000 <= 2,500); at 1,800 the next would not.
    cases = [f.case(i) for i in range(1, 6)]
    script = [_haiku_result(1_200) for _ in cases]
    with patch.object(runner, "estimate_call_micros", return_value=1_000):
        report, provider, client = _run(script, cases, cap_usd=0.0025)
    assert len(provider.images) == 3
    cap = report["spend_cap"]
    assert cap["stopped_by_cap"] is True
    assert report["cost"]["total_usd"] == pytest.approx(0.0018)
    assert cap["counted_usd"] <= cap["cap_usd"]
    assert report["run"]["cases_attempted"] == 3
    assert report["run"]["cases_not_run"] == 2
    # The image of a case the cap stopped was never downloaded.
    assert len(client.calls) == 1 + 3


def test_errored_calls_count_the_estimate_against_the_cap():
    cases = [f.case(i) for i in range(1, 6)]
    script = [ProviderTimeoutError("t") for _ in cases]
    with patch.object(runner, "estimate_call_micros", return_value=1_000):
        report, provider, _ = _run(
            script, cases, cap_usd=0.0025, max_consecutive_errors=100
        )
    assert len(provider.images) == 2
    assert report["spend_cap"]["assumed_for_errored_calls_usd"] == pytest.approx(0.002)
    assert report["spend_cap"]["stopped_by_cap"] is True


def test_a_cap_below_one_call_makes_no_call():
    cases = [f.case(1)]
    report, provider, _ = _run([_haiku_result(1)], cases, cap_usd=0.000001)
    assert provider.images == []
    assert report["spend_cap"]["stopped_by_cap"] is True


def test_run_within_the_cap_is_not_stopped():
    cases = [f.case(i) for i in range(1, 4)]
    report, provider, _ = _run([_haiku_result(10) for _ in cases], cases)
    assert len(provider.images) == 3
    assert report["spend_cap"]["stopped_by_cap"] is False


# --- Metrics -------------------------------------------------------------------------


def test_report_metrics_from_adapter_outcomes():
    dune = [{"title": "Dune", "author": "Frank Herbert"}]
    two = dune + [{"title": "Emma", "author": "Jane Austen"}]
    cases = [
        f.case(1, two),
        f.case(2, dune),
        f.case(3, two),
        f.case(4, dune),
        f.case(5, dune),
        f.case(6, dune),
        f.case(7, dune),
        f.case(8, dune),
    ]
    script = [
        # 1: ok, finds both
        f.result(
            [("Dune", "Frank Herbert"), ("Emma", "Jane Austen")],
            input_tokens=1_000,
            output_tokens=100,
            cache_read_tokens=300,
        ),
        # 2: refused
        f.result([], outcome="refused", input_tokens=1_000, output_tokens=5),
        # 3: truncated, with one complete book (stays truncated)
        f.result(
            [("Dune", "Frank Herbert")],
            outcome="truncated",
            input_tokens=1_000,
            output_tokens=2_048,
            cache_write_tokens=290,
        ),
        # 4: truncated with no book (the gateway reports malformed)
        f.result([], outcome="truncated", input_tokens=1_000, output_tokens=2_048),
        # 5: malformed
        f.result([], outcome="malformed", input_tokens=1_000, output_tokens=50),
        # 6: ok, a wrong book
        f.result([("Emma", "Jane Austen")], input_tokens=1_000, output_tokens=40),
        # 7: error
        ScanUnavailableError("down"),
        # 8: ok, found
        f.result([("dune", "F. Herbert")], input_tokens=1_000, output_tokens=40),
    ]
    # The clock is read at the start and end of an answered call, and only
    # at the start of an errored one. Answered latencies: 1..7 s.
    ticks = iter(
        [0.0, 1.0, 0.0, 2.0, 0.0, 3.0, 0.0, 4.0, 0.0, 5.0, 0.0, 6.0, 0.0, 0.0, 7.0]
    )
    report, _, _ = _run(script, cases, clock=lambda: next(ticks))

    out = report["outcomes"]
    assert out["counts"] == {
        "ok": 3,
        "refused": 1,
        "truncated": 2,
        "malformed": 1,
        "error": 1,
    }
    assert out["refusal_rate"] == pytest.approx(1 / 8)
    assert out["truncation_rate"] == pytest.approx(2 / 8)
    assert out["malformed_rate"] == pytest.approx(1 / 8)
    assert out["error_rate"] == pytest.approx(1 / 8)
    assert out["production_malformed_rate"] == pytest.approx(2 / 8)
    assert report["run"]["error_types"] == {"ScanUnavailableError": 1}

    lat = report["latency_s"]
    assert lat["count"] == 7  # the errored call has no latency
    assert lat["mean"] == pytest.approx(4.0)
    assert lat["p95"] == 7.0  # nearest rank: ceil(0.95 * 7) = 7th

    tok = report["tokens"]
    assert tok == {
        "input": 7_000,
        "output": 100 + 5 + 2_048 + 2_048 + 50 + 40 + 40,
        "cache_read": 300,
        "cache_creation": 290,
    }

    acc = report["accuracy"]
    assert acc["books_truth"] == 10
    assert acc["books_predicted"] == 5
    assert acc["books_matched"] == 4  # case 1 (2), case 3 (1), case 8 (1)
    assert acc["precision"] == pytest.approx(4 / 5)
    assert acc["recall"] == pytest.approx(4 / 10)

    expected_cost = sum(
        cost_micros(
            "claude-haiku-5-5",
            CURRENT_PRICE_VERSION,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cache_read_tokens=r.cache_read_tokens,
            cache_write_tokens=r.cache_write_tokens,
        )
        for r in script
        if not isinstance(r, Exception)
    )
    assert report["cost"]["total_usd"] == pytest.approx(expected_cost / 1e6, abs=1e-6)
    assert report["cost"]["per_scan_usd"] == pytest.approx(
        math.ceil(expected_cost / 7) / 1e6, abs=1e-6
    )
    assert report["dataset"] == {
        "dataset_version": "v1",
        "cases_total": 8,
        "books_total": 10,
    }


@pytest.mark.parametrize(
    "values, expected",
    [
        ([], None),
        ([3.0], 3.0),
        ([float(i) for i in range(1, 21)], 19.0),
        ([float(i) for i in range(100, 0, -1)], 95.0),
    ],
)
def test_p95_is_nearest_rank(values, expected):
    assert runner.p95(values) == expected


def test_images_go_through_the_apps_normalisation():
    import io

    from PIL import Image

    img = Image.new("RGB", (3000, 1000), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    big = buf.getvalue()
    _, provider, _ = _run([_haiku_result(1)], [f.case(1)], image=big)
    sent = Image.open(io.BytesIO(provider.images[0]))
    assert sent.format == "JPEG"
    assert max(sent.size) == HAIKU.max_image_long_edge_px


def test_undecodable_image_is_counted_and_skipped():
    report, provider, _ = _run(
        [_haiku_result(1)], [f.case(1), f.case(2)], image=b"\xff\xd8junk"
    )
    assert provider.images == []
    assert report["run"]["cases_invalid_image"] == 2
    assert report["run"]["cases_attempted"] == 0


def test_consecutive_errors_abort_the_run():
    cases = [f.case(i) for i in range(1, 10)]
    script = [ScanUnavailableError("x") for _ in cases]
    report, provider, _ = _run(script, cases, max_consecutive_errors=3)
    assert len(provider.images) == 3
    assert report["run"]["aborted"] == "3 consecutive failed cases"


def test_deadline_is_the_specs_timeout():
    import anyio

    class Slow:
        async def identify(self, _img):
            await anyio.sleep(30)

    spec = replace(HAIKU, timeout_s=1.0)  # the registry's lowest bound
    record = anyio.run(lambda: runner.call_once(Slow(), spec, b"x"))
    assert record.outcome == "error"
    assert record.error_type == "DeadlineExceeded"


def test_the_gateway_builds_the_adapter():
    """Without an injected factory the gateway's own dispatch builds the
    adapter (patched at the gateway boundary)."""
    built = []

    class FakeAnthropic:
        def __init__(self, spec):
            built.append(spec)

        async def identify(self, _img):
            return _haiku_result(1)

    ds, _ = f.dataset(f.objects_for([f.case(1)]))
    import anyio

    with patch.object(gateway, "AnthropicVisionProvider", FakeAnthropic):
        report = anyio.run(lambda: runner.run_eval(HAIKU, ds, cap_usd=1.0))
    assert built == [HAIKU]
    assert report["run"]["calls_answered"] == 1


# --- CLI refusals ----------------------------------------------------------------------


class _Exploding:
    """A dataset that must never be opened."""

    def open(self):  # pragma: no cover - asserted not called
        raise AssertionError("dataset opened")


def test_cli_refuses_without_a_spend_cap(capsys):
    with patch.object(gateway, "AnthropicVisionProvider") as adapter:
        code = cli.main(
            ["--feature", "scan.standard", "--model", "claude-haiku-5-5"],
            dataset=_Exploding(),
        )
    assert code == 2
    assert "--spend-cap-usd" in capsys.readouterr().err
    adapter.assert_not_called()


@pytest.mark.parametrize("cap", ["0", "-5", "nan", "inf", "ten"])
def test_cli_refuses_a_bad_spend_cap(cap):
    with pytest.raises(SystemExit) as info:
        cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                cap,
            ],
            dataset=_Exploding(),
        )
    assert info.value.code == 2


def test_cli_refuses_without_r2_location(monkeypatch, capsys):
    for name in REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    with patch.object(gateway, "AnthropicVisionProvider") as adapter:
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "10",
            ]
        )
    assert code == 2
    err = capsys.readouterr().err
    assert all(name in err for name in REQUIRED_ENV)
    adapter.assert_not_called()


def test_cli_refuses_without_the_provider_key(capsys):
    with patch.object(settings, "anthropic_api_key", ""):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "10",
            ],
            dataset=_Exploding(),
        )
    assert code == 2
    assert "no API key" in capsys.readouterr().err


def test_cli_writes_the_report(tmp_path, capsys):
    ds, _ = f.dataset(f.objects_for([f.case(1)]))

    class FakeAnthropic:
        def __init__(self, spec):
            pass

        async def identify(self, _img):
            return _haiku_result(10)

    out = tmp_path / "report.json"
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", FakeAnthropic),
    ):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "1",
                "--report",
                str(out),
            ],
            dataset=ds,
        )
    assert code == 0
    report = json.loads(out.read_text())
    assert report["model"] == "claude-haiku-5-5"
    assert report["accuracy"]["recall"] == 1.0
    assert "precision=1.000" in capsys.readouterr().out


def test_cli_unexpected_failure_reports_type_only(capsys):
    class Broken:
        def open(self):
            raise RuntimeError("secret-bucket/secret-key")

    with patch.object(settings, "anthropic_api_key", "test-key"):
        code = cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                "1",
            ],
            dataset=Broken(),
        )
    assert code == 3
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret" not in err


# --- Early stops keep the report (review should-fix 2) --------------------------


def _cli_args(report, *extra):
    return [
        "--feature",
        "scan.standard",
        "--model",
        "claude-haiku-5-5",
        "--spend-cap-usd",
        "1",
        "--report",
        str(report),
        *extra,
    ]


def test_unreadable_image_is_counted_and_skipped():
    cases = [f.case(i) for i in range(1, 4)]
    objects = f.objects_for(cases)
    del objects[cases[1]["image"]]  # R2 read fails for case 2
    ds, _ = f.dataset(objects)
    provider = f.ScriptedProvider([_haiku_result(10), _haiku_result(10)])
    import anyio

    report = anyio.run(
        lambda: runner.run_eval(
            HAIKU, ds, cap_usd=1.0, provider_factory=lambda _s: provider
        )
    )
    assert len(provider.images) == 2
    assert report["run"]["cases_unreadable"] == 1
    assert report["run"]["cases_attempted"] == 2
    assert report["run"]["cases_not_run"] == 0
    assert report["run"]["aborted"] is None


def test_disk_error_is_counted_as_unreadable(monkeypatch):
    from eval.ai_scan import dataset as dataset_mod

    def full(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(dataset_mod.os, "fdopen", full)
    report, provider, _ = _run([_haiku_result(1)] * 2, [f.case(1), f.case(2)])
    assert provider.images == []
    assert report["run"]["cases_unreadable"] == 2


def test_consecutive_unreadable_images_abort():
    cases = [f.case(i) for i in range(1, 6)]
    ds, _ = f.dataset({"manifest.json": f.manifest(cases)})  # no images at all
    import anyio

    report = anyio.run(
        lambda: runner.run_eval(
            HAIKU,
            ds,
            cap_usd=1.0,
            provider_factory=lambda _s: f.ScriptedProvider([]),
            max_consecutive_errors=2,
        )
    )
    assert report["run"]["cases_unreadable"] == 2
    assert report["run"]["aborted"] == "2 consecutive failed cases"


class _InterruptOnSecond:
    calls = 0

    def __init__(self, spec):
        pass

    async def identify(self, _img):
        _InterruptOnSecond.calls += 1
        if _InterruptOnSecond.calls == 2:
            raise KeyboardInterrupt
        return _haiku_result(1_000)


def test_ctrl_c_still_writes_the_partial_report(tmp_path, capsys):
    _InterruptOnSecond.calls = 0
    ds, _ = f.dataset(f.objects_for([f.case(i) for i in range(1, 5)]))
    out = tmp_path / "report.json"
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", _InterruptOnSecond),
    ):
        code = cli.main(_cli_args(out), dataset=ds)
    assert code == 130
    report = json.loads(out.read_text())
    assert report["run"]["aborted"] == "interrupted"
    assert report["run"]["cases_attempted"] == 1
    assert report["cost"]["total_usd"] > 0
    assert "interrupted" in capsys.readouterr().err


def test_unexpected_error_mid_run_still_writes_the_report(tmp_path, capsys):
    cases = [f.case(i) for i in range(1, 4)]
    ds, _ = f.dataset(f.objects_for(cases))
    real = runner.normalise_image
    seen = []

    def explode_second(raw, **kw):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("secret-key in a message")
        return real(raw, **kw)

    class OneOk:
        def __init__(self, spec):
            pass

        async def identify(self, _img):
            return _haiku_result(1_000)

    out = tmp_path / "report.json"
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", OneOk),
        patch.object(runner, "normalise_image", explode_second),
    ):
        code = cli.main(_cli_args(out), dataset=ds)
    assert code == 3
    report = json.loads(out.read_text())
    assert report["run"]["aborted"] == "error (RuntimeError)"
    assert report["run"]["cases_attempted"] == 1
    assert report["cost"]["total_usd"] > 0
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "secret-key" not in err


def test_no_report_before_the_manifest_is_read(tmp_path):
    ds, _ = f.dataset({})  # no manifest
    out = tmp_path / "report.json"
    with patch.object(settings, "anthropic_api_key", "test-key"):
        code = cli.main(_cli_args(out), dataset=ds)
    assert code == 2
    assert not out.exists()


def test_aborted_run_exits_1_with_report(tmp_path):
    ds, _ = f.dataset(f.objects_for([f.case(i) for i in range(1, 4)]))

    class Down:
        def __init__(self, spec):
            pass

        async def identify(self, _img):
            raise ScanUnavailableError("down")

    out = tmp_path / "report.json"
    with (
        patch.object(settings, "anthropic_api_key", "test-key"),
        patch.object(gateway, "AnthropicVisionProvider", Down),
    ):
        code = cli.main(_cli_args(out, "--max-consecutive-errors", "2"), dataset=ds)
    assert code == 1
    assert json.loads(out.read_text())["run"]["aborted"] == "2 consecutive failed cases"


# --- CLI limits ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_max_consecutive_errors_must_be_at_least_one(value):
    with pytest.raises(SystemExit) as info:
        cli.main(
            _cli_args("r.json", "--max-consecutive-errors", value), dataset=_Exploding()
        )
    assert info.value.code == 2


def test_run_refuses_max_consecutive_errors_below_one():
    ds, _ = f.dataset({})
    with pytest.raises(EvalError, match="at least 1"):
        runner.EvalRun(HAIKU, ds, cap_usd=1.0, max_consecutive_errors=0)


@pytest.mark.parametrize("cap", ["100.01", "1000"])
def test_cli_refuses_a_spend_cap_above_the_maximum(cap):
    with pytest.raises(SystemExit) as info:
        cli.main(
            [
                "--feature",
                "scan.standard",
                "--model",
                "claude-haiku-5-5",
                "--spend-cap-usd",
                cap,
            ],
            dataset=_Exploding(),
        )
    assert info.value.code == 2


def test_runner_refuses_a_spend_cap_above_the_maximum():
    assert runner.cap_micros_from_usd(100) == 100_000_000
    with pytest.raises(EvalError, match="at most 100"):
        runner.cap_micros_from_usd(100.01)


# --- Accuracy over answered calls ---------------------------------------------------


def test_recall_over_answered_calls_excludes_provider_errors():
    cases = [f.case(1), f.case(2)]
    script = [f.result([("Dune", "Frank Herbert")]), ScanUnavailableError("down")]
    report, _, _ = _run(script, cases)
    assert report["accuracy"]["recall"] == 0.5
    answered = report["accuracy_answered_calls"]
    assert answered["recall"] == 1.0
    assert answered["precision"] == 1.0
    assert answered["images_scored"] == 1


def test_fixture_restores_the_eval_logger_level():
    import logging

    assert "eval.ai_scan" in f.LOGGERS_TO_RESTORE
    assert logging.getLogger("eval.ai_scan") is cli.logger
