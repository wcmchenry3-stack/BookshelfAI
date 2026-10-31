"""Author-name comparison for matching a reading against provider records."""

from app.services.book_identifier import BookCandidate
from app.services.enrichment.text import _fold, _normalise

# Name suffixes skipped when taking a surname ("Martin Luther King Jr.").
_NAME_SUFFIXES = frozenset(
    {"jr", "sr", "ii", "iii", "iv", "phd", "md", "pere", "père", "fils"}
)


# (initials of the given names, surname, last two words joined, the
# second-to-last word of a name of 3 or more words).
_NameParts = tuple[list[str], str, str, str]


# Company suffixes: "Lonely Planet Publications, Inc." is not "Inc. ...".
_COMPANY_SUFFIXES = frozenset({"inc", "ltd", "llc", "co", "corp", "plc", "gmbh"})


def _suffix_or_date(word: str) -> bool:
    return word in _NAME_SUFFIXES or word in _COMPANY_SUFFIXES or word.isdigit()


def _name_parts(name: object) -> _NameParts:
    """One name, normalised and without suffixes or dates: "David M.
    Potter" is (["d", "m"], "potter", "mpotter"). "Last, First" is swapped
    ("Herbert, Frank", "Smith, John, Jr.", "Brown, Dan, 1964-"), but not
    when the part after the first comma is a suffix ("Martin Luther King,
    Jr.", "Lonely Planet Publications, Inc.") or the parts after it are not
    all suffixes or dates."""
    if isinstance(name, str) and "," in name:
        last, first, *rest = name.split(",")
        first_words = _normalise(first).split()
        rest_words = _normalise(" ".join(rest)).split()
        if (
            first_words
            and not all(_suffix_or_date(w) for w in first_words)
            and all(_suffix_or_date(w) for w in rest_words)
        ):
            name = f"{first} {last}"
    words = [w for w in _normalise(name).split() if not _suffix_or_date(w)]
    if not words or words == ["unknown"]:
        return [], "", "", ""
    joined = "".join(words[-2:]) if len(words) > 1 else ""
    penult = words[-2] if len(words) >= 3 else ""
    return [w[0] for w in words[:-1]], words[-1], joined, penult


def _names_agree(a: _NameParts, b: _NameParts) -> bool:
    """The same surname (or one side's last two words joined are the other's
    surname: "Le Guin" and "LeGuin") and, when both give a first name or
    initial, the first initial of one among the other's initials ("David
    Potter" and "David Morris Potter", "M. L. King" and "Martin Luther King
    Jr.", but not "Frank Herbert" and "Brian Herbert"). A compound surname
    also agrees with its first part: one side's surname may be the other's
    second-to-last word when that name has 3 or more words ("Miguel de
    Cervantes" and "Miguel de Cervantes Saavedra")."""
    a_initials, a_surname, a_joined, a_penult = a
    b_initials, b_surname, b_joined, b_penult = b
    if not a_surname or not b_surname:
        return False
    if a_surname != b_surname:
        if b_surname == a_joined:
            a_initials = a_initials[:-1]  # "Le" is part of the surname
        elif a_surname == b_joined:
            b_initials = b_initials[:-1]
        elif a_surname == b_penult:
            b_initials = b_initials[:-1]  # "Cervantes" is part of the surname
        elif b_surname == a_penult:
            a_initials = a_initials[:-1]
        else:
            return False
    if not a_initials or not b_initials:
        return True
    return a_initials[0] in b_initials or b_initials[0] in a_initials


def reading_surname(candidate: BookCandidate) -> str:
    """The surname of the author the model read ("David M. Potter",
    "David Potter" and "Potter" all give "potter"): the author key of a
    title search (#743)."""
    return _name_parts(candidate.author)[1]


def _record_names(authors: object) -> list[_NameParts]:
    if not isinstance(authors, list):
        return []
    return [n for n in (_name_parts(_fold(a)) for a in authors) if n[1]]


def _author_contradicted(candidate: BookCandidate, authors: object) -> bool:
    """Whether the reading and the record both name an author and no record
    author is the reading's."""
    reading_name = _name_parts(_fold(candidate.author))
    record_names = _record_names(authors)
    if not reading_name[1] or not record_names:
        return False
    return not any(_names_agree(reading_name, n) for n in record_names)
