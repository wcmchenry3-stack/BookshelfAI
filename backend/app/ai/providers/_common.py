"""Reply parsing shared by the vision adapters (#814).

Each adapter (``anthropic_vision``, ``openai_vision``) keeps its own rules
where they differ on purpose; only the pieces that were word for word the
same live here:

- the title/author length limit and the structural "broken item" check
  (:func:`has_broken_text`);
- confidence clamping to 0-1 (:func:`clamp_confidence`). What counts as a
  confidence differs by adapter and stays in each adapter: Claude gives 0.5
  for anything that is not a number, OpenAI parses a numeric string and
  treats any other non-number as broken. The difference is intentional
  until #795 decides; do not unify it here;
- ISBN cleaning (:func:`clean_isbn_13`, :func:`clean_isbn_10`, #746);
- token counts (:func:`token_count`);
- the de-duplicating candidate loop (:func:`collect_candidates`).

Each adapter keeps its own content-free log wrapper (``_log_content``,
``_log_unparseable``): ``tests/unit/test_claude_policy.py`` checks the
Claude adapter's wrapper in place.
"""

import math
import re
from collections.abc import Callable, Hashable, Iterable
from typing import Any

from app.services.book_identifier import BookCandidate

# The ledger's token columns are Postgres INTEGER.
MAX_TOKEN_COUNT = 2_147_483_647
# Title/author are search keys; anything longer is not a real title.
MAX_TEXT_LENGTH = 500


# ---------------------------------------------------------------------------
# ISBNs (#746)
# ---------------------------------------------------------------------------

# A vision model can invent an ISBN: it looks right but fails the check digit.
# Such a value wastes provider lookups and can point at the wrong book, so both
# adapters drop it (the book itself is kept).
_ISBN_SEPARATORS = re.compile(r"[\s-]")


def clean_isbn_13(value: object) -> str | None:
    """``value`` as 13 ASCII digits with a valid checksum (spaces and hyphens
    removed), else None. A non-string is None."""
    if not isinstance(value, str):
        return None
    digits = _ISBN_SEPARATORS.sub("", value)
    if len(digits) != 13 or not digits.isascii() or not digits.isdigit():
        return None
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return digits if total % 10 == 0 else None


def clean_isbn_10(value: object) -> str | None:
    """``value`` as 10 characters (the last may be ``X``) with a valid
    checksum (spaces and hyphens removed), else None. A non-string is None."""
    if not isinstance(value, str):
        return None
    chars = _ISBN_SEPARATORS.sub("", value).upper()
    if (
        len(chars) != 10
        or not chars.isascii()
        or not chars[:9].isdigit()
        or not (chars[9].isdigit() or chars[9] == "X")
    ):
        return None
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(chars))
    return chars if total % 11 == 0 else None


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def has_broken_text(item: object) -> bool:
    """Whether ``item`` breaks the requested structure (#572): not an object,
    or a title or author that is missing, not a string, or longer than
    ``MAX_TEXT_LENGTH`` once stripped (#754). An empty or whitespace title or
    author is not broken: the item is schema-valid and only dropped."""
    if not isinstance(item, dict):
        return True
    for key in ("title", "author"):
        value = item.get(key)
        if not isinstance(value, str) or len(value.strip()) > MAX_TEXT_LENGTH:
            return True
    return False


def clamp_confidence(value: float) -> float:
    """A numeric confidence clamped to 0-1 (#754): a boolean or NaN is 0.5,
    infinity clamps to 0 or 1. An integer is compared exactly, because
    ``float()`` overflows past ~1e308. Callers decide what a non-number is
    (see the module docstring and #795)."""
    if isinstance(value, bool):
        return 0.5
    if isinstance(value, int):
        return float(min(max(value, 0), 1))
    if math.isnan(value):
        return 0.5
    return min(max(float(value), 0.0), 1.0)


def collect_candidates(
    items: Iterable[Any],
    max_books: int,
    *,
    is_broken: Callable[[Any], bool],
    to_candidate: Callable[[Any], BookCandidate | None],
    dedupe_key: Callable[[BookCandidate], Hashable],
) -> tuple[list[BookCandidate], int]:
    """The candidates in ``items`` (at most ``max_books``) and how many
    items were broken. A broken item is counted and skipped; an item
    ``to_candidate`` turns into None is skipped (schema-valid but dropped);
    a candidate whose ``dedupe_key`` was already seen is skipped. Stops at
    ``max_books`` candidates without looking at the rest."""
    candidates: list[BookCandidate] = []
    seen: set[Hashable] = set()
    broken = 0
    for item in items:
        if len(candidates) >= max_books:
            break
        if is_broken(item):
            broken += 1
            continue
        candidate = to_candidate(item)
        if candidate is None:
            continue
        key = dedupe_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates, broken


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


def token_count(value: object) -> int:
    """A token count from a usage block: an int clamped to
    ``[0, MAX_TOKEN_COUNT]``; anything else is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_TOKEN_COUNT)
