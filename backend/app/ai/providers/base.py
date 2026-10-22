"""Provider interfaces used by the gateway."""

from dataclasses import dataclass
from typing import Literal, Protocol

from app.services.book_identifier import BookCandidate, ScanUnavailableError

# How a provider call that returned a response ended (#530). Calls that raised
# never produce a ProviderResult; the caller records those (timeout, error...).
ProviderOutcome = Literal["ok", "refused", "truncated"]


class ProviderTimeoutError(ScanUnavailableError):
    """The provider call timed out: the provider's own HTTP timeout, or the
    gateway's overall deadline. A ``ScanUnavailableError``, so ``/scan``
    still refunds it; the subclass only lets the ledger record ``timeout``."""


@dataclass(frozen=True)
class ProviderResult:
    """What one provider call returned: the books and the billed usage.

    The token counts are disjoint, so the cost is a plain sum of each count
    times its price (``app.ai.prices``):

    - ``input_tokens``: prompt tokens billed at the full input price (cached
      prompt tokens are NOT included here; they are ``cache_read_tokens``);
    - ``output_tokens``: completion tokens;
    - ``cache_read_tokens``: prompt tokens served from the provider's prompt
      cache;
    - ``cache_write_tokens``: prompt tokens written to the provider's cache
      (0 for OpenAI, which does not bill cache writes separately).

    A missing or garbled usage block gives zeros; it never raises.
    """

    candidates: list[BookCandidate]
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    outcome: ProviderOutcome = "ok"


class VisionProvider(Protocol):
    """Identifies books in one image.

    A provider is built for one :class:`~app.ai.registry.FeatureSpec` and takes
    its model, token cap and timeout from that spec only.

    Raises ``ProviderTimeoutError`` (a ``ScanUnavailableError``) when the
    provider times out and ``ScanUnavailableError`` when it returns an HTTP
    error. ``httpx.ConnectError`` (never connected) propagates as-is.
    """

    async def identify(self, image_bytes: bytes) -> ProviderResult: ...
