"""The eval run: each labelled photo through one provider adapter, scored,
under a hard spend cap, summed into an aggregate report.

The call path matches production up to the adapter: the photo goes through
``app.ai.images.normalise_image`` with the feature's long-edge cap and the
app's pixel cap (the same normalisation ``/scan`` applies, so token counts
match), then the adapter that ``app.ai.gateway`` builds for the spec, under
the gateway's overall deadline (``spec.timeout_s``), and its outcome is read
with ``gateway.reported_outcome``. It does NOT go through ``/scan``: no
database, quota, breaker, scan records or usage ledger. The harness never
imports a provider SDK or ``app.ai.providers``; the gateway builds the
adapter.

Cost is computed with the ledger's own pricing function
(``app.ai.prices.cost_micros`` at ``CURRENT_PRICE_VERSION``) from each
call's real reported usage, exactly as ``app.ai.ledger`` would price it.
There are no ledger rows: the eval has no app database.
"""

from __future__ import annotations

import io
import logging
import math
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import anyio
from PIL import Image

from app.ai import gateway
from app.ai.images import ImageNormalisationError, normalise_image
from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.prices import (
    CURRENT_PRICE_VERSION,
    TOKENS_PER_PRICE_UNIT,
    cost_micros,
    price_for,
)
from app.ai.registry import FeatureSpec, get_feature
from app.core.config import settings
from eval.ai_scan import EvalError
from eval.ai_scan.dataset import Manifest, R2Dataset
from eval.ai_scan.image_tokens import (
    NoImageTokenRule,
    image_tokens,
    rule_for,
    worst_case_image_tokens,
)
from eval.ai_scan.scoring import (
    MATCHING_RULE,
    Book,
    ImageScore,
    aggregate,
    score_image,
    truth_book,
)

logger = logging.getLogger(__name__)

REPORT_VERSION = 1
MICROS_PER_USD = 1_000_000

# Upper bound on the non-image prompt tokens of one call: the system prompt
# (about 300 tokens), the user text and, for Claude, the structured-output
# schema are well under this. The image's tokens are bounded per model by
# eval.ai_scan.image_tokens.
PROMPT_TOKEN_BOUND = 2_000

# The largest --spend-cap-usd accepted (the workflow's default is 10).
MAX_SPEND_CAP_USD = 100

DEFAULT_MAX_CONSECUTIVE_ERRORS = 5

ProviderFactory = Callable[[FeatureSpec], Any]


# --- Spec ---------------------------------------------------------------------


def infer_provider(model: str) -> str:
    """The provider whose allow-list holds ``model``."""
    providers = [p for p, models in ALLOWED_MODELS.items() if model in models]
    if len(providers) != 1:
        raise EvalError(
            "unknown model: it is not in app.ai.models_allowlist.ALLOWED_MODELS"
        )
    return providers[0]


def resolve_spec(feature: str, model: str, provider: str | None = None) -> FeatureSpec:
    """The registry spec of ``feature`` with ``provider``/``model`` swapped
    in. ``replace`` re-runs the registry's validation, so only allow-listed
    models run, with the registry's max_tokens, effort, timeout and image
    size."""
    provider = provider or infer_provider(model)
    try:
        base = get_feature(feature)
        spec = replace(base, provider=provider, model=model, enabled=True)
    except (LookupError, ValueError) as exc:
        raise EvalError(f"invalid feature/provider/model: {exc}") from None
    try:
        rule_for(spec.model)
    except NoImageTokenRule:
        # Fail closed: a call whose cost cannot be bounded never runs.
        raise EvalError(
            "refusing to run: no image-token rule for this model, so the spend "
            "cap cannot bound a call (add one in eval/ai_scan/image_tokens.py)"
        ) from None
    return spec


def provider_configured(spec: FeatureSpec) -> bool:
    """Whether the app settings hold an API key for ``spec.provider``."""
    return bool(getattr(settings, f"{spec.provider}_api_key", ""))


# --- Spend cap ------------------------------------------------------------------


def estimate_call_micros(
    spec: FeatureSpec, width: int | None = None, height: int | None = None
) -> int:
    """An upper bound on one call's cost in micro-dollars.

    Prompt tokens: the image's bound for this model (from the normalised
    image's ``width`` x ``height``, or the worst case for any image within
    the feature's long-edge cap when they are not given) plus
    ``PROMPT_TOKEN_BOUND``, all at the model's highest input rate (input,
    cache read or cache write). Output: a full ``max_tokens`` reply. Never
    less than the registry's own per-call estimate
    (``est_cost_ceiling_micros``)."""
    if width is None or height is None:
        img = worst_case_image_tokens(spec.model, spec.max_image_long_edge_px)
    else:
        img = image_tokens(spec.model, width, height)
    price = price_for(spec.model, CURRENT_PRICE_VERSION)
    input_rate = max(price.input, price.cache_read, price.cache_write)
    scaled = (img + PROMPT_TOKEN_BOUND) * input_rate
    scaled += spec.max_tokens * price.output
    bound = -(-scaled // TOKENS_PER_PRICE_UNIT)
    return max(bound, spec.est_cost_ceiling_micros)


def cap_micros_from_usd(cap_usd: float) -> int:
    if (
        isinstance(cap_usd, bool)
        or not isinstance(cap_usd, (int, float))
        or not math.isfinite(cap_usd)
        or cap_usd <= 0
    ):
        raise EvalError("--spend-cap-usd must be a positive number")
    if cap_usd > MAX_SPEND_CAP_USD:
        raise EvalError(f"--spend-cap-usd must be at most {MAX_SPEND_CAP_USD}")
    return math.floor(cap_usd * MICROS_PER_USD)


@dataclass
class SpendCap:
    """Hard cap. ``spent`` is the priced usage of answered calls; ``assumed``
    is the bound charged for each errored call (a timeout or dropped
    connection may still have been billed). ``reserved`` holds the bound of
    each call in flight: it is reserved BEFORE the provider is awaited and
    settled when the call returns (replaced by the real cost, or moved to
    ``assumed`` for an error); a call interrupted mid-await (Ctrl-C,
    cancellation) keeps its reservation, since it may have been billed. A
    call is only made when ``spent + assumed + reserved + its bound <= cap``.
    ``estimate`` is the worst-case bound for any image (reported);
    ``max_bound`` the largest bound a call was actually checked against."""

    cap: int
    estimate: int
    spent: int = 0
    assumed: int = 0
    reserved: int = 0
    max_bound: int = 0
    stopped: bool = False

    @property
    def total(self) -> int:
        return self.spent + self.assumed + self.reserved

    def allows(self, bound: int) -> bool:
        return self.total + bound <= self.cap


# --- One call -------------------------------------------------------------------


@dataclass
class CallRecord:
    outcome: str  # adapter outcome: ok/refused/truncated/malformed, or "error"
    reported: str  # gateway.reported_outcome (what /scan acts on), or "error"
    predictions: list[Book] = field(default_factory=list)
    latency_s: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_micros: int = 0
    error_type: str | None = None


async def call_once(
    provider: Any,
    spec: FeatureSpec,
    image_bytes: bytes,
    clock: Callable[[], float] = time.perf_counter,
) -> CallRecord:
    """One adapter call under the gateway's overall deadline."""
    start = clock()
    try:
        with anyio.move_on_after(spec.timeout_s) as deadline:
            result = await provider.identify(image_bytes)
        if deadline.cancelled_caught:
            return CallRecord("error", "error", error_type="DeadlineExceeded")
    except Exception as exc:  # noqa: BLE001 - recorded by type name only
        return CallRecord("error", "error", error_type=type(exc).__name__)
    latency = clock() - start
    cost = cost_micros(
        spec.model,
        CURRENT_PRICE_VERSION,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
    )
    return CallRecord(
        outcome=result.outcome,
        reported=gateway.reported_outcome(result),
        predictions=[
            Book(c.title, c.author, isbn_13=c.isbn_13, isbn_10=c.isbn_10)
            for c in result.candidates
        ],
        latency_s=latency,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        cost_micros=cost,
    )


# --- Report ---------------------------------------------------------------------


def p95(values: list[float]) -> float | None:
    """Nearest-rank 95th percentile: the ceil(0.95 n)-th smallest value."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(0.95 * len(ordered)), 1) - 1]


def _usd(micros: int) -> float:
    return round(micros / MICROS_PER_USD, 6)


def _rate(count: int, total: int) -> float | None:
    return count / total if total else None


def build_report(
    *,
    spec: FeatureSpec,
    dataset_version: str,
    cases_total: int,
    books_total: int,
    records: list[CallRecord],
    scores: list[ImageScore],
    invalid_images: int,
    unreadable: int,
    interrupted: int,
    cap: SpendCap,
    aborted: str | None,
    started_at: datetime,
) -> dict[str, Any]:
    """The aggregate report. Counts, rates, tokens, cost and the run's
    settings only: no image, EXIF, title, author, case id, object key,
    bucket or endpoint. ``scores[i]`` belongs to ``records[i]``."""
    attempted = len(records)  # calls that returned (answered or errored)
    outcomes = Counter(r.outcome for r in records)
    reported = Counter(r.reported for r in records)
    answered = [r for r in records if r.outcome != "error"]
    answered_scores = [s for r, s in zip(records, scores) if r.outcome != "error"]
    latencies = [r.latency_s for r in answered if r.latency_s is not None]
    errors = Counter(r.error_type for r in records if r.error_type)
    answered_agg = aggregate(answered_scores)
    return {
        "report_version": REPORT_VERSION,
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "feature": spec.name,
        "provider": spec.provider,
        "model": spec.model,
        "effort": spec.effort,
        "max_tokens": spec.max_tokens,
        "max_image_long_edge_px": spec.max_image_long_edge_px,
        "timeout_s": spec.timeout_s,
        "price_version": CURRENT_PRICE_VERSION,
        "dataset": {
            "dataset_version": dataset_version,
            "cases_total": cases_total,
            "books_total": books_total,
        },
        "run": {
            # Every call made, including one interrupted mid-await; the
            # outcome rates below are over the calls that returned.
            "cases_attempted": attempted + interrupted,
            "cases_invalid_image": invalid_images,
            "cases_unreadable": unreadable,
            "cases_not_run": (
                cases_total - attempted - interrupted - invalid_images - unreadable
            ),
            "calls_interrupted": interrupted,
            "calls_answered": len(answered),
            "calls_errored": outcomes.get("error", 0),
            "error_types": dict(sorted(errors.items())),
            "aborted": aborted,
        },
        # Over every attempted call (an errored call predicted nothing: what
        # the user would have got), and over answered calls only (the
        # model's accuracy, without provider errors).
        "accuracy": {"matching_rule": MATCHING_RULE, **aggregate(scores)},
        "accuracy_answered_calls": {
            "precision": answered_agg["precision"],
            "recall": answered_agg["recall"],
            "f1": answered_agg["f1"],
            "images_scored": answered_agg["images_scored"],
        },
        "outcomes": {
            "counts": {
                k: outcomes.get(k, 0)
                for k in ("ok", "refused", "truncated", "malformed", "error")
            },
            "refusal_rate": _rate(outcomes.get("refused", 0), attempted),
            "truncation_rate": _rate(outcomes.get("truncated", 0), attempted),
            "malformed_rate": _rate(outcomes.get("malformed", 0), attempted),
            "error_rate": _rate(outcomes.get("error", 0), attempted),
            # What /scan acts on: a truncated reply with no complete book
            # is reported as malformed (503, refunded).
            "production_malformed_rate": _rate(reported.get("malformed", 0), attempted),
        },
        "latency_s": {
            "method": "answered calls only; p95 is nearest-rank",
            "count": len(latencies),
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p95": p95(latencies),
        },
        "tokens": {
            "input": sum(r.input_tokens for r in records),
            "output": sum(r.output_tokens for r in records),
            "cache_read": sum(r.cache_read_tokens for r in records),
            "cache_creation": sum(r.cache_write_tokens for r in records),
        },
        "cost": {
            "source": (
                "app.ai.prices.cost_micros (the usage ledger's pricing) applied "
                "to each answered call's reported usage; no ledger rows"
            ),
            "total_usd": _usd(cap.spent),
            "per_scan_usd": (
                _usd(math.ceil(cap.spent / len(answered))) if answered else None
            ),
        },
        "spend_cap": {
            "cap_usd": _usd(cap.cap),
            "worst_case_call_bound_usd": _usd(cap.estimate),
            "largest_call_bound_used_usd": _usd(cap.max_bound),
            "assumed_for_errored_calls_usd": _usd(cap.assumed),
            "reserved_for_interrupted_calls_usd": _usd(cap.reserved),
            "counted_usd": _usd(cap.total),
            "stopped_by_cap": cap.stopped,
        },
    }


# --- The run ----------------------------------------------------------------------


def _jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


class EvalRun:
    """One eval run. :meth:`execute` runs it; :meth:`report` builds the
    report from whatever has run so far, so a run that stops early (an
    error, Ctrl-C) still reports what was spent. ``report()`` is None until
    the manifest has been read (before that nothing is spent)."""

    def __init__(
        self,
        spec: FeatureSpec,
        dataset: R2Dataset,
        *,
        cap_usd: float,
        provider_factory: ProviderFactory | None = None,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        if max_consecutive_errors < 1:
            raise EvalError("--max-consecutive-errors must be at least 1")
        self.spec = spec
        self.dataset = dataset
        self.cap = SpendCap(
            cap=cap_micros_from_usd(cap_usd), estimate=estimate_call_micros(spec)
        )
        self.provider_factory = provider_factory
        self.max_consecutive_errors = max_consecutive_errors
        self.clock = clock
        self.started_at = datetime.now(UTC)
        self.records: list[CallRecord] = []
        self.scores: list[ImageScore] = []
        self.invalid_images = 0
        self.unreadable = 0
        self.interrupted = 0
        self.aborted: str | None = None
        self.manifest: Manifest | None = None

    def report(self) -> dict[str, Any] | None:
        if self.manifest is None:
            return None
        cases = self.manifest.cases
        return build_report(
            spec=self.spec,
            dataset_version=self.manifest.dataset_version,
            cases_total=len(cases),
            books_total=sum(len(c.books) for c in cases),
            records=self.records,
            scores=self.scores,
            invalid_images=self.invalid_images,
            unreadable=self.unreadable,
            interrupted=self.interrupted,
            cap=self.cap,
            aborted=self.aborted,
            started_at=self.started_at,
        )

    async def execute(self) -> dict[str, Any]:
        """Run every case (in manifest order) until done, the spend cap would
        be exceeded by the next call, or ``max_consecutive_errors`` cases in a
        row failed (an errored call or an unreadable image)."""
        spec, cap = self.spec, self.cap
        # The gateway's own adapter dispatch, looked up at call time (tests
        # patch the adapter classes on app.ai.gateway, as the app's tests do).
        provider = (self.provider_factory or gateway._build_provider)(spec)
        consecutive = 0
        # The bound for the smallest possible image: when even that does not
        # fit, stop before downloading anything more.
        smallest_bound = estimate_call_micros(spec, 1, 1)
        with self.dataset.open() as ds:
            self.manifest = ds.manifest()
            cases = self.manifest.cases
            total = len(cases)
            for index, case in enumerate(cases, start=1):
                if not cap.allows(smallest_bound):
                    cap.stopped = True
                    logger.warning(
                        "spend cap reached: stopping before case %d/%d", index, total
                    )
                    break
                try:
                    raw = ds.image(index, case)
                except (EvalError, OSError) as exc:
                    self.unreadable += 1
                    consecutive += 1
                    logger.warning(
                        "case %d/%d: image unreadable (%s)",
                        index,
                        total,
                        type(exc).__name__,
                    )
                    if consecutive >= self.max_consecutive_errors:
                        self.aborted = f"{consecutive} consecutive failed cases"
                        break
                    continue
                try:
                    image_bytes = await anyio.to_thread.run_sync(
                        lambda raw=raw: normalise_image(
                            raw,
                            max_long_edge_px=spec.max_image_long_edge_px,
                            max_pixels=settings.scan_max_image_pixels,
                        )
                    )
                except ImageNormalisationError as exc:
                    self.invalid_images += 1
                    logger.warning(
                        "case %d/%d: image rejected (%s)", index, total, exc.code
                    )
                    continue
                bound = estimate_call_micros(spec, *_jpeg_size(image_bytes))
                if not cap.allows(bound):
                    cap.stopped = True
                    logger.warning(
                        "spend cap reached: stopping before case %d/%d", index, total
                    )
                    break
                cap.max_bound = max(cap.max_bound, bound)
                # Reserve the bound before awaiting the provider, so a call
                # interrupted mid-await (Ctrl-C, cancellation) is still counted.
                cap.reserved += bound
                try:
                    record = await call_once(provider, spec, image_bytes, self.clock)
                except BaseException:
                    # KeyboardInterrupt, cancellation: the call may have been
                    # billed, so its reservation stays in the counted spend.
                    self.interrupted += 1
                    raise
                cap.reserved -= bound
                if record.outcome == "error":
                    cap.assumed += bound
                    consecutive += 1
                else:
                    cap.spent += record.cost_micros
                    consecutive = 0
                truth = [truth_book(b.title, b.author, b.isbn) for b in case.books]
                self.records.append(record)
                self.scores.append(score_image(record.predictions, truth))
                logger.info(
                    "case %d/%d: %s%s",
                    index,
                    total,
                    record.outcome,
                    f" ({record.error_type})" if record.error_type else "",
                )
                if consecutive >= self.max_consecutive_errors:
                    self.aborted = f"{consecutive} consecutive failed cases"
                    logger.error("aborting: %s", self.aborted)
                    break
        report = self.report()
        if report is None:  # pragma: no cover - the manifest was read above
            raise EvalError("no manifest")
        return report


async def run_eval(
    spec: FeatureSpec,
    dataset: R2Dataset,
    *,
    cap_usd: float,
    provider_factory: ProviderFactory | None = None,
    max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Build and execute an :class:`EvalRun`; return its report."""
    run = EvalRun(
        spec,
        dataset,
        cap_usd=cap_usd,
        provider_factory=provider_factory,
        max_consecutive_errors=max_consecutive_errors,
        clock=clock,
    )
    return await run.execute()
