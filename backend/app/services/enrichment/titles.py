"""Title comparison: main title / subtitle, edition notes, volume markers."""

import re

from app.services.enrichment.text import _fold, _normalise, normalise_title

# Where a subtitle or edition note starts: "Dune: Deluxe Edition",
# "Dune (40th Anniversary)", "Dune - A Novel", "Dune — Book One".
_SUBTITLE = re.compile(r"\s*[:(\[\u2013\u2014]|\s+-\s+")
# A subtitle that only describes the edition, not which book it is. Anything
# else ("Book Two", "The Two Towers") may tell volumes of a series apart, so it
# must match exactly.
_EDITION_NOTE = re.compile(
    r"^(?:(?:a|an|the)\s+)?(?:\d+(?:st|nd|rd|th)\s+)?"
    r"(?:(?:deluxe|special|collectors?|anniversary|illustrated|annotated|"
    r"unabridged|abridged|revised|expanded|updated|definitive|complete|"
    r"classic|movie\s+tie\s*in|tie\s*in|mass\s+market|large\s+print|"
    r"paperback|hardcover|reissue|new|first|second|third)\s+)*"
    r"(?:edition|ed|anniversary|novel|classic|reissue|paperback|hardcover|"
    r"printing)$"
)


def _split_title(value: object) -> tuple[str, str]:
    """(normalised main title, normalised subtitle or "")."""
    if not isinstance(value, str):
        return "", ""
    parts = _SUBTITLE.split(value, maxsplit=1)
    main = normalise_title(parts[0])
    sub = _normalise(parts[1]) if len(parts) > 1 else ""
    return main, sub


def titles_agree(a: object, b: object) -> bool:
    """Whether two provider titles name the same book: equal main titles
    where each subtitle is the same or only an edition note ("Dune" vs
    "Dune: Deluxe Edition"), so a series' volumes ("Saga: Book One" vs
    "Saga: Book Two") never agree."""
    main_a, sub_a = _split_title(a)
    main_b, sub_b = _split_title(b)
    if not main_a or main_a != main_b:
        return False
    if sub_a == sub_b:
        return True
    return all(not sub or _EDITION_NOTE.match(sub) for sub in (sub_a, sub_b))


def _full_title(record: dict) -> object:
    """A record's title with its separate ``subtitle`` (Google Books
    ``volumeInfo``, Open Library docs since #743) folded in as "title:
    subtitle", so a distinguishing subtitle is compared too."""
    title, subtitle = record.get("title"), record.get("subtitle")
    if isinstance(title, str) and isinstance(subtitle, str) and subtitle.strip():
        return f"{title}: {subtitle}"
    return title


def _gb_full_title(info: dict) -> object:
    """Google Books' title with its ``subtitle`` folded in."""
    return _full_title(info)


# Where a title's head ends when a reading is matched against a record: the
# subtitle separators above plus a comma or semicolon.
_HEAD = re.compile(r"\s*[,;:(\[–—]|\s+-\s+")
# A rest of a record's title that only dates the book: "1848-1861", "1990".
_DATES = re.compile(r"^\d{4}(?: \d{4})*$")


def _head_and_rest(value: object) -> tuple[str, str]:
    """(normalised title before the first comma or subtitle separator,
    normalised rest or "")."""
    if not isinstance(value, str):
        return "", ""
    parts = _HEAD.split(value, maxsplit=1)
    rest = _normalise(parts[1]) if len(parts) > 1 else ""
    return normalise_title(parts[0]), rest


# A (normalised) subtitle that tells volumes of a series apart. Only a
# volume *position* counts: a marker word followed by a volume number
# ("Book Two", "Vol 3", "Part II", "Episode IV A New Hope", "Dune
# Chronicles Book 1"), a subtitle that is only a volume number ("II", "2";
# "#2" is "2" once normalised), or a trailing 1-3 digit number ("Dune
# Chronicles 1"). A marker word elsewhere ("A Memoir in Books", "Being the
# First Part of the Lord of the Rings", "How to Say No"), a numeral inside a
# phrase ("A World War II Story", "Pope John Paul II", "12 More Rules"), a
# 4-digit year ("Since 1945") or an ordinal ("14th Century") is not one.
_ROMAN = r"(?=[ivx])x{0,3}(?:ix|iv|v?i{0,3})"
_NUM_WORDS = (
    r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen"
    r"|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|first"
    r"|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh"
    r"|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth"
    r"|eighteenth|nineteenth|twentieth|final|last"
)
_NUM = rf"(?:\d{{1,3}}|{_ROMAN}|{_NUM_WORDS}|one)"
_NUM_NOT_ONE = rf"(?:\d{{1,3}}|{_ROMAN}|{_NUM_WORDS})"
_VOLUME_MARKER = re.compile(
    # "Book the First" as well as "Book One".
    rf"(?:^|\s)(?:books?|volumes?|vols?|parts?|tomes?|episodes?|pt|bk)\s+"
    rf"(?:the\s+)?{_NUM}(?=\s|$)"
    rf"|(?:^|\s)(?:no|number)\s+{_NUM_NOT_ONE}(?=\s|$)"
    rf"|^{_NUM}$"
    r"|\s\d{1,3}$"
)


def _is_volume_marker(subtitle: str) -> bool:
    return bool(_VOLUME_MARKER.search(subtitle))


def _listed_same_book(reading_title: object, record_title: object) -> bool:
    """For a record that lists the scanned ISBN: the same main title, unless
    the record's subtitle is a volume marker and the reading carries a
    different subtitle or volume ("Saga: Book One" vs "Saga: Book Two").
    The listed ISBN is trusted otherwise, whatever the subtitles say."""
    main_r, sub_r = _split_title(_fold(reading_title))
    main_b, sub_b = _split_title(_fold(record_title))
    if not main_r or main_r != main_b:
        return False
    return not (_is_volume_marker(sub_b) and sub_r and sub_r != sub_b)


def _one_sided_subtitle(a: object, b: object) -> bool:
    """Whether two titles have the same main title and at most one of them a
    subtitle, which is not a volume marker ("Sapiens" and "Sapiens: A Brief
    History of Humankind", never "Saga: Book One" and "Saga: Book Two")."""
    main_a, sub_a = _split_title(_fold(a))
    main_b, sub_b = _split_title(_fold(b))
    if not main_a or main_a != main_b or (sub_a and sub_b):
        return False
    return not _is_volume_marker(sub_a or sub_b)
