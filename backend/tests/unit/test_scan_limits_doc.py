"""Drift guard for docs/scan-limits.md (#581).

The page is the one reference for every scan limit and dial. A key added to
the ``ai_settings`` registry, or a ``rate_limit_*`` setting, must be mentioned
there, or this test fails and names what is missing.
"""

from pathlib import Path

import pytest

from app.ai import settings as ai_settings
from app.core.config import Settings

DOC = Path(__file__).resolve().parents[3] / "docs" / "scan-limits.md"
HOW_TO_FIX = (
    "add it to a table in docs/scan-limits.md (one row: limit, what it limits, "
    "applies to, default, setting, bounds, what the user sees, where it is "
    "tracked)"
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    assert DOC.is_file(), f"{DOC} is missing"
    return DOC.read_text(encoding="utf-8")


def _mentioned(name: str, text: str) -> bool:
    return f"`{name}`" in text or f"`{name} " in text or f"{name}`" in text


@pytest.mark.parametrize("key", ai_settings.KNOWN_KEYS)
def test_every_registry_key_is_in_the_scan_limits_doc(key, doc_text):
    assert _mentioned(key, doc_text), (
        f"ai_settings key {key!r} (app/ai/settings.py) is not in "
        f"docs/scan-limits.md: {HOW_TO_FIX}"
    )


RATE_LIMIT_FIELDS = sorted(
    f for f in Settings.model_fields if f.startswith("rate_limit_")
)


def test_rate_limit_settings_exist():
    assert RATE_LIMIT_FIELDS, "no rate_limit_* settings found; fix this test"


@pytest.mark.parametrize("field", RATE_LIMIT_FIELDS)
def test_every_rate_limit_setting_is_in_the_scan_limits_doc(field, doc_text):
    assert _mentioned(field, doc_text), (
        f"settings field {field!r} (app/core/config.py) is not in "
        f"docs/scan-limits.md: {HOW_TO_FIX}"
    )


def test_retired_keys_are_not_listed_as_live_limits(doc_text):
    """Retired keys may appear, but only under the "Retired" heading."""
    live, _, _retired = doc_text.partition("## Retired")
    for key in ai_settings.RETIRED_KEYS:
        assert f"`{key}`" not in live, (
            f"retired key {key!r} appears above the Retired section of "
            "docs/scan-limits.md"
        )
