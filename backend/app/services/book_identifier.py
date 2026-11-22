"""Shared types for book identification."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class BookCandidate:
    title: str
    author: str
    confidence: float  # 0.0 – 1.0
    isbn_13: str | None = None
    isbn_10: str | None = None


class ScanUnavailableError(Exception):
    """Raised when the vision API is unreachable or times out."""


# The most records one Open Library / Google Books title search asks for
# (#743).
MAX_SEARCH_RESULTS = 5


def best_record(
    records: list[dict], score: Callable[[dict], int] | None
) -> dict | None:
    """The first record, or with ``score`` the highest-scoring one whose score
    is positive (ties: the earlier one); None when there is none."""
    if score is None:
        return records[0] if records else None
    best, best_score = None, 0
    for record in records:
        value = score(record)
        if value > best_score:
            best, best_score = record, value
    return best
