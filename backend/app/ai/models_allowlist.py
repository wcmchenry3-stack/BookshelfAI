"""Single source of truth for which models and limits the AI layer accepts.

Kept free of other ``app`` imports so ``app.core.config`` (the scan bounds)
and ``app.ai.registry`` (validates every spec when the registry is built) can
import it without a cycle. Anthropic is the only provider.
"""

from types import MappingProxyType

# Models each provider may be asked for. Anything else is rejected.
ALLOWED_MODELS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        # Fixed model IDs (#548, option a): Anthropic publishes no dated
        # snapshot for these models; the IDs below are the pinned versions,
        # not moving aliases. Never add a ``-latest`` or other moving name.
        # tests/unit/test_anthropic_vision.py checks this set exactly.
        "anthropic": frozenset(
            {
                "claude-haiku-5-5",
                "claude-sonnet-5-5",
            }
        ),
    }
)

# Inclusive bounds: (min, max).
MAX_TOKENS_BOUNDS = (1, 16_384)
TIMEOUT_S_BOUNDS = (1.0, 120.0)
