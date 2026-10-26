"""Run the labelled scan evaluation (#536) for one feature and model.

From ``backend/``::

    python -m eval.ai_scan.run_eval --feature scan.standard \\
        --model claude-haiku-5-5 --spend-cap-usd 10 --report ai-eval-report.json

(``python eval/ai_scan/run_eval.py ...`` works too.) ``--spend-cap-usd`` is
required. The dataset location and its read-only credential come from the
``AI_EVAL_R2_*`` environment variables; the provider's API key from the
app settings' usual environment variable. Writes only the aggregate JSON
report (also, partial, when a run that has started stops early).
``--spend-cap-usd`` is at most 100. Exit codes: 0 finished (also when the
spend cap stopped it; see ``spend_cap.stopped_by_cap``), 1 aborted after
repeated failed cases, 2 refused or failed with a reported reason, 3
unexpected failure (type name only), 130 interrupted.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:  # run as a script
    sys.path.insert(0, str(BACKEND_DIR))
# The app settings require a database URL at import; the eval never connects.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://unused@localhost/unused")

import anyio

from eval.ai_scan import EvalError
from eval.ai_scan.dataset import R2Dataset, r2_config_from_env
from eval.ai_scan.runner import (
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    MAX_SPEND_CAP_USD,
    EvalRun,
    provider_configured,
    resolve_spec,
)

logger = logging.getLogger("eval.ai_scan")

# Loggers that would print dataset content at DEBUG/INFO: Pillow logs every
# EXIF tag it parses, botocore/boto3/s3transfer/urllib3 log request
# parameters (bucket, key, headers), the SDKs and HTTP clients log request
# options (the anthropic SDK includes the base64 image at debug). Pinned to
# WARNING whatever the root level is.
QUIET_LOGGERS = (
    "PIL",
    "boto3",
    "botocore",
    "s3transfer",
    "urllib3",
    "httpx",
    "httpcore",
    "httpx2",
    "anthropic",
)


def quiet_third_party_loggers() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _positive_usd(value: str) -> float:
    try:
        amount = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number of US dollars") from None
    if not math.isfinite(amount) or amount <= 0:
        raise argparse.ArgumentTypeError("must be a positive number of US dollars")
    if amount > MAX_SPEND_CAP_USD:
        raise argparse.ArgumentTypeError(f"must be at most {MAX_SPEND_CAP_USD}")
    return amount


def _at_least_one(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="run_eval",
        description="Labelled scan evaluation of one model (#536).",
    )
    ap.add_argument("--feature", required=True, help="scan.standard or scan.enhanced")
    ap.add_argument("--model", required=True, help="an allow-listed model ID")
    ap.add_argument(
        "--provider",
        default=None,
        help="anthropic or openai (default: the provider whose allow-list has the model)",
    )
    ap.add_argument(
        "--spend-cap-usd",
        type=_positive_usd,
        default=None,
        help=(
            "REQUIRED hard spend cap in US dollars, at most "
            f"{MAX_SPEND_CAP_USD} (the workflow defaults to 10)"
        ),
    )
    ap.add_argument("--report", default="ai-eval-report.json", type=Path)
    ap.add_argument(
        "--max-consecutive-errors",
        type=_at_least_one,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
    )
    return ap


def _summary(report: dict) -> str:
    acc, out, cost, cap = (
        report["accuracy"],
        report["outcomes"],
        report["cost"],
        report["spend_cap"],
    )

    def fmt(v: object) -> str:
        return f"{v:.3f}" if isinstance(v, float) else str(v)

    return (
        f"{report['feature']} {report['model']}: "
        f"precision={fmt(acc['precision'])} recall={fmt(acc['recall'])} "
        f"truncation={fmt(out['truncation_rate'])} "
        f"malformed={fmt(out['malformed_rate'])} "
        f"refusal={fmt(out['refusal_rate'])} "
        f"p95={fmt(report['latency_s']['p95'])}s "
        f"cost/scan=${fmt(cost['per_scan_usd'])} total=${fmt(cost['total_usd'])} "
        f"stopped_by_cap={cap['stopped_by_cap']}"
    )


def _write_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


def main(argv: list[str] | None = None, *, dataset: R2Dataset | None = None) -> int:
    """``dataset`` is injected by the tests; otherwise R2 from the env.

    Once the manifest has been read, a report is ALWAYS written, also when
    the run stops early (an error, Ctrl-C): ``run.aborted`` says why, and the
    spend so far is in it. Errors are reported by type name only."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    logger.setLevel(logging.INFO)
    quiet_third_party_loggers()
    run: EvalRun | None = None
    code = 0
    try:
        if args.spend_cap_usd is None:
            raise EvalError("refusing to run without --spend-cap-usd")
        if dataset is None:
            dataset = R2Dataset(r2_config_from_env())
        spec = resolve_spec(args.feature, args.model, args.provider)
        if not provider_configured(spec):
            raise EvalError(
                f"refusing to run: no API key is configured for {spec.provider}"
            )
        run = EvalRun(
            spec,
            dataset,
            cap_usd=args.spend_cap_usd,
            max_consecutive_errors=args.max_consecutive_errors,
        )
        anyio.run(run.execute)
        if run.aborted:
            code = 1
    except KeyboardInterrupt:
        print("ai-eval: interrupted", file=sys.stderr)
        if run is not None:
            run.aborted = "interrupted"
        code = 130
    except EvalError as exc:
        print(f"ai-eval: {exc}", file=sys.stderr)
        if run is not None:
            run.aborted = "error (EvalError)"
        code = 2
    except Exception as exc:  # noqa: BLE001 - its message may hold a key or title
        print(f"ai-eval: failed ({type(exc).__name__})", file=sys.stderr)
        if run is not None:
            run.aborted = f"error ({type(exc).__name__})"
        code = 3
    finally:
        report = run.report() if run is not None else None
        if report is not None:
            _write_report(args.report, report)
            print(_summary(report))
    return code


if __name__ == "__main__":
    sys.exit(main())
