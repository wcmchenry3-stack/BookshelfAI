"""Single source of truth for which models and limits the AI layer accepts.

Kept free of other ``app`` imports so both ``app.core.config`` (validates the
env vars at settings load) and ``app.ai.registry`` (validates again when the
registry is built) can import it without a cycle.
"""

from types import MappingProxyType

# Models each provider may be asked for. Anything else is rejected.
ALLOWED_MODELS: MappingProxyType[str, frozenset[str]] = MappingProxyType(
    {
        "openai": frozenset(
            {
                "gpt-4o-mini",
                "gpt-4o-mini-2024-07-18",
                "gpt-4o",
                "gpt-4o-2024-08-06",
                "gpt-4o-2024-11-20",
            }
        ),
    }
)

# Inclusive bounds: (min, max).
MAX_TOKENS_BOUNDS = (1, 16_384)
TIMEOUT_S_BOUNDS = (1.0, 120.0)
