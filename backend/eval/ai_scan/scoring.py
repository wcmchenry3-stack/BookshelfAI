"""Deterministic scoring of one scan against its ground truth. No LLM judge.

A predicted book MATCHES a ground-truth book when either:

1. ISBN: both carry an ISBN and they name the same ISBN-13 (an ISBN-10 is
   converted to its 978 ISBN-13 with the app's ``normalise_isbn_13``; a
   prediction's ISBN-13 and ISBN-10 are both tried); or
2. title + author: the titles agree by the app's own rule
   (``app.services.enrichment.titles_agree``: ``normalise_title`` on the main
   title, so case, punctuation, spacing and a leading "The/A/An" are ignored,
   and a differing subtitle only counts when it is more than an edition note,
   so "Dune" matches "Dune: Deluxe Edition" but "Saga: Book One" does not
   match "Saga: Book Two"), AND the authors agree: their
   ``normalise_text`` forms are equal, or their last words (the surname:
   "Frank Herbert" and "F. Herbert") are equal and non-empty.

Before matching, duplicate ground-truth entries of one book (the same
normalised title and author, or a shared ISBN) are collapsed: both adapters
drop duplicate predictions, so two copies on a shelf can only ever be
predicted once.

Matching is one-to-one: a maximum bipartite matching between the scan's
predictions and the image's ground truth, so a prediction counts for at most
one book and a book is found at most once. A repeated prediction is a false
positive. A prediction that could match two different books counts once.

Per image: ``matched`` true positives, ``predicted - matched`` false
positives, ``truth - matched`` missed books.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# app.schemas first: importing app.services.enrichment on its own trips an
# import cycle (enrichment -> app.schemas -> user_book -> enrichment).
import app.schemas  # noqa: F401
from app.services.enrichment import (
    normalise_isbn_13,
    normalise_text,
    normalise_title,
    titles_agree,
)

MATCHING_RULE = "isbn-or-title+author/one-to-one/dedup-truth/v2"


@dataclass(frozen=True)
class Book:
    """A book to compare: a prediction or a ground-truth entry."""

    title: str
    author: str
    isbn_13: str | None = None
    isbn_10: str | None = None


def truth_book(title: str, author: str, isbn: str | None) -> Book:
    """A ground-truth book; its single ``isbn`` may be an ISBN-10 or -13."""
    if isbn and len(isbn) == 10:
        return Book(title, author, isbn_10=isbn)
    return Book(title, author, isbn_13=isbn)


def _isbn_keys(book: Book) -> set[str]:
    keys = {
        normalise_isbn_13(book.isbn_13, None),
        normalise_isbn_13(None, book.isbn_10),
    }
    keys.discard(None)
    return keys  # type: ignore[return-value]


def authors_agree(a: str, b: str) -> bool:
    na, nb = normalise_text(a), normalise_text(b)
    if not na or not nb:
        return False
    return na == nb or na.split()[-1] == nb.split()[-1]


def books_match(predicted: Book, truth: Book) -> bool:
    if _isbn_keys(predicted) & _isbn_keys(truth):
        return True
    return titles_agree(predicted.title, truth.title) and authors_agree(
        predicted.author, truth.author
    )


def match_count(predicted: Sequence[Book], truth: Sequence[Book]) -> int:
    """The size of a maximum one-to-one matching (Kuhn's augmenting paths;
    deterministic, at most ~100 x 100 per image)."""
    edges = [[j for j, t in enumerate(truth) if books_match(p, t)] for p in predicted]
    owner: list[int | None] = [None] * len(truth)

    def augment(i: int, seen: set[int]) -> bool:
        for j in edges[i]:
            if j in seen:
                continue
            seen.add(j)
            current = owner[j]
            if current is None or augment(current, seen):
                owner[j] = i
                return True
        return False

    return sum(1 for i in range(len(predicted)) if augment(i, set()))


@dataclass(frozen=True)
class ImageScore:
    predicted: int
    truth: int
    matched: int

    @property
    def precision(self) -> float | None:
        return self.matched / self.predicted if self.predicted else None

    @property
    def recall(self) -> float | None:
        return self.matched / self.truth if self.truth else None


def _same_book(a: Book, b: Book) -> bool:
    """Two ground-truth entries for one book: the same normalised title and
    author, or a shared ISBN."""
    if _isbn_keys(a) & _isbn_keys(b):
        return True
    return normalise_title(a.title) == normalise_title(b.title) and normalise_text(
        a.author
    ) == normalise_text(b.author)


def collapse_truth(truth: Sequence[Book]) -> list[Book]:
    """The ground truth with duplicate entries of one book removed (the
    first is kept). Both adapters drop duplicate predictions (same
    casefolded title and author), so a book listed twice could never be
    found twice; counting it twice would only lower recall."""
    kept: list[Book] = []
    for book in truth:
        if not any(_same_book(book, k) for k in kept):
            kept.append(book)
    return kept


def score_image(predicted: Sequence[Book], truth: Sequence[Book]) -> ImageScore:
    truth = collapse_truth(truth)
    return ImageScore(len(predicted), len(truth), match_count(predicted, truth))


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(scores: Sequence[ImageScore]) -> dict[str, float | int | None]:
    """Micro precision/recall/F1 over all books, and the per-image means
    (an image with no predictions has no precision and one with no
    ground-truth books has no recall; each is left out of that mean)."""
    predicted = sum(s.predicted for s in scores)
    truth = sum(s.truth for s in scores)
    matched = sum(s.matched for s in scores)
    precision = _ratio(matched, predicted)
    recall = _ratio(matched, truth)
    # F1 = 2 TP / (2 TP + FP + FN) = 2 matched / (predicted + truth): 0.0
    # when nothing was predicted but there were books; None only when there
    # was nothing on either side.
    f1 = 2 * matched / (predicted + truth) if predicted + truth else None
    return {
        "images_scored": len(scores),
        "books_predicted": predicted,
        "books_truth": truth,
        "books_matched": matched,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "per_image_mean_precision": _mean(
            [s.precision for s in scores if s.precision is not None]
        ),
        "per_image_mean_recall": _mean(
            [s.recall for s in scores if s.recall is not None]
        ),
    }
