"""What a scan's provider call settles: the cost the breaker adds to the
day's spend (#531) and the usage-ledger row (#530). Both builders never
raise, so they cannot turn a scan into an error or mask the original
exception."""

import logging
import uuid

from sqlalchemy.sql import Executable

from app.ai import breaker, ledger, quota
from app.ai.gateway import AIResult, resolve_spec
from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
from app.ai.settings import AISettings
from app.models.ai_usage import STATUS_ERROR
from app.services.scan import SCAN_LOGGER

logger = logging.getLogger(SCAN_LOGGER)


def build_settlement(
    *,
    reservation: quota.Reservation,
    feature: str,
    ai: AISettings,
    status: str | None = None,
    result: AIResult | None = None,
) -> breaker.Settlement:
    """What this call adds to the day's spend (#531). With ``result`` (the
    provider answered) its priced cost; otherwise (``error`` or ``timeout``:
    real cost unknown) the reservation's estimate, so the breaker errs on the
    safe side.
    A call whose cost cannot be priced also settles the estimate. Never
    raises."""
    estimate = reservation.est_cost_micros
    if result is not None:
        try:
            cost = cost_micros(
                result.model,
                CURRENT_PRICE_VERSION,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
            )
        except Exception:
            logger.exception("scan: could not price the call; settling the estimate")
            cost = estimate
        return breaker.Settlement(cost, result.feature, result.model, result.outcome)
    try:
        model = resolve_spec(feature, ai).model
    except Exception:  # noqa: BLE001 - only an alert tag; never fail the settle
        model = "unknown"
    return breaker.Settlement(estimate, feature, model, status or STATUS_ERROR)


def build_usage_row(
    *,
    call_id: uuid.UUID,
    user_id: uuid.UUID,
    feature: str,
    ai: AISettings,
    status: str | None = None,
    result: AIResult | None = None,
    attempt: int,
) -> Executable | None:
    """The ledger insert for this scan's provider call (#530), or None.

    With ``result`` (the provider answered) the status is its outcome
    (``ok``/``refused``/``truncated``/``malformed``) and the tokens are its
    usage; otherwise
    ``status`` is given and the tokens are 0. ``attempt`` (#603) is the
    request's validated ``attempt`` field, stored as is. Never raises: a ledger problem
    (e.g. a model with no price) is logged and the row skipped, so it cannot
    turn a scan into an error or mask the original exception.
    """
    try:
        if result is not None:
            return ledger.usage_insert(
                call_id=call_id,
                user_id=user_id,
                feature=result.feature,
                model=result.model,
                status=result.outcome,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cache_read_tokens=result.cache_read_tokens,
                cache_write_tokens=result.cache_write_tokens,
                request_id=ledger.current_request_id(),
                attempt=attempt,
            )
        return ledger.usage_insert(
            call_id=call_id,
            user_id=user_id,
            feature=feature,
            # The same resolution run_feature used (same settings snapshot).
            model=resolve_spec(feature, ai).model,
            status=status,
            request_id=ledger.current_request_id(),
            attempt=attempt,
        )
    except Exception:
        logger.exception("scan: could not build the usage ledger row")
        return None
