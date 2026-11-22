"""Scoring a provider record against the model's reading."""

from collections.abc import Callable

from app.services.book_identifier import BookCandidate
from app.services.enrichment.authors import (
    _name_parts,
    _names_agree,
    _record_names,
)
from app.services.enrichment.isbn import _gb_isbns, _ol_isbns
from app.services.enrichment.text import _fold, _provider_text
from app.services.enrichment.titles import (
    _DATES,
    _EDITION_NOTE,
    _full_title,
    _gb_full_title,
    _head_and_rest,
    _is_volume_marker,
    _one_sided_subtitle,
    _split_title,
    titles_agree,
)


def records_agree(ol_doc: dict, gb_volume: dict) -> bool:
    """Whether an Open Library doc and a Google Books volume describe the
    same book: they share an ISBN, or their titles agree (:func:`titles_agree`:
    the same main title, any differing subtitle only an edition note) and
    their first-author surnames match (and are non-empty)."""
    if _ol_isbns(ol_doc) & _gb_isbns(gb_volume):
        return True
    info = gb_volume.get("volumeInfo") or {}
    if not isinstance(info, dict):
        return False
    # Since #743 the first authors are compared as names (:func:`_names_agree`:
    # "Herbert, Frank" is "Frank Herbert", diacritics ignored), and a
    # subtitle on one side only that is not a volume marker still agrees.
    ol_names, gb_names = _record_names(ol_doc.get("author_name")), _record_names(
        info.get("authors")
    )
    if not ol_names or not gb_names or not _names_agree(ol_names[0], gb_names[0]):
        return False
    ol_title, gb_title = _fold(_full_title(ol_doc)), _fold(_gb_full_title(info))
    return titles_agree(ol_title, gb_title) or _one_sided_subtitle(ol_title, gb_title)


def reading_match_score(
    candidate: BookCandidate, title: object, authors: object
) -> int:
    """How well a provider record (its title and author list) fits the
    model's reading of the photo (#743); 0 means it is not that book.

    Title, one of:

    - the titles agree (:func:`titles_agree`: the same main title, any
      differing subtitle only an edition note), so "Dune" never fits "Dune:
      Messiah" and "Saga" never fits "Saga: Book Two";
    - with the author confirmed, the record's title is the reading's title
      followed only by dates or an edition note after a comma or subtitle
      separator ("The Impending Crisis" fits "The impending crisis,
      1848-1861"), unless the reading's own subtitle names something else
      ("Saga: Book Two" never fits "Saga: Deluxe Edition");
    - lowest, with the author confirmed, the same main title where only one
      side has a subtitle and it is not a volume marker ("Sapiens" fits
      "Sapiens: A Brief History of Humankind"; "Saga" never fits "Saga: Book
      Two"). A spine often leaves the subtitle out, and Open Library and
      Google Books often disagree on whether it belongs to the title. This
      tier also lets "Dune" fit "Dune: Messiah" when nothing better is
      found; better matches always score higher.

    Author: when the reading and a record author share the surname and both
    give a first name or initial, the first initials must agree as well
    (:func:`_names_agree`), so "Frank Herbert" never fits "Brian Herbert".
    When the reading names an author, the record must name that author; when
    it names none, any author passes. A record that names no author fits only
    on agreeing titles. A confirmed author adds 1 to the score. A reading
    without a title fits nothing. Diacritics are ignored throughout.

    This only chooses which record is used: every catalog field of a match
    still comes from the record (#602). It cannot prove the record is the
    book in the photo: a wrong reading can still fit another real book."""
    reading_title, title = _fold(candidate.title), _fold(title)
    reading_main, reading_sub = _split_title(reading_title)
    record_main, _ = _split_title(title)
    if not reading_main or not record_main:
        return 0
    reading_name = _name_parts(_fold(candidate.author))
    record_names = _record_names(authors)
    agree = titles_agree(reading_title, title)
    if not record_names:
        return 6 if agree else 0
    author_confirmed = bool(reading_name[1]) and any(
        _names_agree(reading_name, n) for n in record_names
    )
    if reading_name[1] and not author_confirmed:
        return 0
    if agree:
        return 6 + int(author_confirmed)
    if not author_confirmed:
        return 0
    head, rest = _head_and_rest(title)
    # The reading's own subtitle must not name something else: "Saga: Book
    # Two" never fits "Saga: Deluxe Edition" (the unnumbered work).
    reading_sub_ok = (
        not reading_sub
        or bool(_DATES.match(reading_sub))
        or bool(_EDITION_NOTE.match(reading_sub))
    )
    if (
        reading_sub_ok
        and head == reading_main
        and (_DATES.match(rest) or _EDITION_NOTE.match(rest))
    ):
        return 3
    if _one_sided_subtitle(reading_title, title):
        return 2
    return 0


# What the matching reads from a record: (usable display title or None,
# title with the subtitle folded in, author list).
_RecordParts = tuple[str | None, object, object]


def _ol_parts(doc: object) -> _RecordParts:
    if not isinstance(doc, dict):
        return None, None, None
    return _provider_text(doc.get("title")), _full_title(doc), doc.get("author_name")


def _gb_parts(volume: object) -> _RecordParts:
    info = volume.get("volumeInfo") if isinstance(volume, dict) else None
    if not isinstance(info, dict):
        return None, None, None
    return _provider_text(info.get("title")), _full_title(info), info.get("authors")


def _scorer(
    candidate: BookCandidate, parts: Callable[[object], _RecordParts]
) -> Callable[[dict], int]:
    """:func:`reading_match_score` for one provider's records (0 for a
    record without a usable title), on its title with the subtitle folded
    in. When the reading has no subtitle and the record's is not a volume
    marker, the bare title is scored too, capped at the lowest tier (2), so
    a reading "Sapiens" with no
    author still fits a record "Sapiens" + subtitle, but an exact title
    always ranks higher and "Saga" never fits "Saga" + "Book Two"."""

    def score(record: dict) -> int:
        display, full, authors = parts(record)
        if not display:
            return 0
        best = reading_match_score(candidate, full, authors)
        if full != display and not _split_title(_fold(candidate.title))[1]:
            _, subtitle = _split_title(_fold(full))
            if subtitle and not _is_volume_marker(subtitle):
                best = max(
                    best, min(2, reading_match_score(candidate, display, authors))
                )
        return best

    return score
