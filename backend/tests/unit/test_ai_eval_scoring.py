"""Scan evaluation scoring (#536): deterministic one-to-one matching."""

import pytest

from eval.ai_scan.scoring import (
    Book,
    ImageScore,
    aggregate,
    books_match,
    collapse_truth,
    match_count,
    score_image,
    truth_book,
)

DUNE = truth_book("Dune", "Frank Herbert", "9780441013593")
HOBBIT = truth_book("The Hobbit", "J.R.R. Tolkien", None)


def test_normalisation_leading_article_case_and_punctuation():
    assert books_match(Book("hobbit!", "J. R. R. Tolkien"), HOBBIT)
    assert books_match(Book("  THE   HOBBIT ", "j.r.r. tolkien"), HOBBIT)


def test_same_surname_counts_as_the_same_author():
    assert books_match(Book("Dune", "F. Herbert"), DUNE)


def test_wrong_author_or_title_does_not_match():
    assert not books_match(Book("Dune", "Isaac Asimov"), DUNE)
    assert not books_match(Book("Dune Messiah", "Frank Herbert"), DUNE)


def test_subtitle_rule_is_the_apps():
    assert books_match(Book("Dune: Deluxe Edition", "Frank Herbert"), DUNE)
    saga_one = truth_book("Saga: Book One", "Brian Vaughan", None)
    assert not books_match(Book("Saga: Book Two", "Brian Vaughan"), saga_one)


def test_isbn_only_match_ignores_a_wrong_title():
    # ISBN-10 form of DUNE's ISBN-13.
    assert books_match(Book("Unreadable spine", "?", isbn_10="0441013597"), DUNE)
    assert books_match(Book("x", "y", isbn_13="978-0441013593"), DUNE)
    # Different ISBN but same title + author still matches (another edition).
    assert books_match(Book("Dune", "Frank Herbert", isbn_13="9780593099322"), DUNE)


def test_isbn_10_ground_truth():
    truth = truth_book("Dune", "Frank Herbert", "0441013597")
    assert books_match(Book("?", "?", isbn_13="9780441013593"), truth)


def test_duplicate_predictions_count_once():
    preds = [Book("Dune", "Frank Herbert"), Book("Dune", "Frank Herbert")]
    score = score_image(preds, [DUNE])
    assert (score.predicted, score.truth, score.matched) == (2, 1, 1)
    assert score.precision == 0.5
    assert score.recall == 1.0


def test_duplicate_truth_entries_are_collapsed():
    # Both adapters drop duplicate predictions, so a book listed twice could
    # never be found twice: it counts once.
    preds = [Book("Dune", "Frank Herbert")]
    score = score_image(preds, [DUNE, truth_book("dune", "FRANK HERBERT", None)])
    assert (score.truth, score.matched) == (1, 1)
    assert score.recall == 1.0


def test_truth_entries_sharing_an_isbn_are_collapsed():
    a = truth_book("Dune", "Frank Herbert", "9780441013593")
    b = truth_book("Dune (Penguin)", "F. Herbert", "0441013597")
    assert collapse_truth([a, b]) == [a]


def test_different_books_are_not_collapsed():
    emma = truth_book("Emma", "Jane Austen", None)
    messiah = truth_book("Dune Messiah", "Frank Herbert", None)
    assert collapse_truth([DUNE, emma, messiah]) == [DUNE, emma, messiah]


def test_a_prediction_matching_two_truths_counts_once():
    # The prediction matches "Dune Messiah" by ISBN and "Dune" by title.
    messiah = truth_book("Dune Messiah", "Frank Herbert", "9780441013593")
    plain = truth_book("Dune", "Frank Herbert", None)
    pred = Book("Dune", "Frank Herbert", isbn_13="9780441013593")
    assert books_match(pred, messiah) and books_match(pred, plain)
    score = score_image([pred], [messiah, plain])
    assert (score.truth, score.matched) == (2, 1)
    assert score.recall == 0.5


def test_matching_is_maximum_not_greedy():
    # p1 matches both truths, p2 only the first. Greedy (p1 -> t1) finds 1;
    # the maximum matching (p1 -> t2, p2 -> t1) finds 2.
    t1 = truth_book("Dune Messiah", "Frank Herbert", "9780441013593")
    t2 = truth_book("Dune", "Frank Herbert", None)
    p1 = Book("Dune", "Frank Herbert", isbn_13="9780441013593")
    p2 = Book("smudged", "?", isbn_13="9780441013593")
    assert match_count([p1, p2], [t1, t2]) == 2


def test_empty_sides():
    assert score_image([], [DUNE]) == ImageScore(0, 1, 0)
    assert score_image([], [DUNE]).precision is None
    assert score_image([Book("Dune", "Frank Herbert")], []).recall is None


def test_aggregate_micro_and_per_image():
    scores = [
        ImageScore(predicted=4, truth=5, matched=3),
        ImageScore(predicted=1, truth=5, matched=1),
        ImageScore(predicted=0, truth=2, matched=0),  # no precision
        ImageScore(predicted=2, truth=0, matched=0),  # no recall
    ]
    agg = aggregate(scores)
    assert agg["books_predicted"] == 7
    assert agg["books_truth"] == 12
    assert agg["books_matched"] == 4
    assert agg["precision"] == pytest.approx(4 / 7)
    assert agg["recall"] == pytest.approx(4 / 12)
    p, r = 4 / 7, 4 / 12
    assert agg["f1"] == pytest.approx(2 * p * r / (p + r))
    assert agg["per_image_mean_precision"] == pytest.approx((3 / 4 + 1 + 0) / 3)
    assert agg["per_image_mean_recall"] == pytest.approx((3 / 5 + 1 / 5 + 0) / 3)


def test_aggregate_with_nothing():
    agg = aggregate([])
    assert agg["precision"] is None and agg["recall"] is None and agg["f1"] is None


def test_aggregate_zero_matches_gives_zero_f1():
    agg = aggregate([ImageScore(predicted=2, truth=2, matched=0)])
    assert agg["f1"] == 0.0


def test_f1_is_zero_when_nothing_was_predicted():
    agg = aggregate([ImageScore(predicted=0, truth=3, matched=0)])
    assert agg["precision"] is None
    assert agg["recall"] == 0.0
    assert agg["f1"] == 0.0
