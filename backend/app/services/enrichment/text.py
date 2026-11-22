"""Text normalisation shared by the enrichment matching modules."""

import re
import unicodedata

# EnrichedBook's limit for title / author.
_MAX_TEXT = 500


def _provider_text(value: object) -> str | None:
    """A usable provider string (non-empty, within the schema limit)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if 0 < len(value) <= _MAX_TEXT else None


def _first_text(values: object) -> str | None:
    if isinstance(values, list) and values:
        return _provider_text(values[0])
    return None


_NON_WORD = re.compile(r"[\W_]+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an) ")


def _normalise(value: object) -> str:
    """Casefolded, punctuation stripped, whitespace collapsed."""
    if not isinstance(value, str):
        return ""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(_NON_WORD.sub(" ", value).split())


def normalise_text(value: object) -> str:
    """Public form of :func:`_normalise`: casefolded, punctuation stripped,
    whitespace collapsed."""
    return _normalise(value)


def normalise_title(value: object) -> str:
    """A title for comparison: :func:`_normalise`, minus a leading article."""
    return _LEADING_ARTICLE.sub("", _normalise(value), count=1)


# Letters NFKD does not decompose, folded for comparison ("Nesbø").
_FOLD_LETTERS = str.maketrans(
    {"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "æ": "ae", "Æ": "AE", "œ": "oe"}
    | {"Œ": "OE", "đ": "d", "Đ": "D", "ı": "i", "ß": "ss"}
)


def _fold(value: object) -> object:
    """``value`` without diacritics (NFKD, combining marks dropped, and the
    letters in ``_FOLD_LETTERS`` spelled out), for comparing a reading with
    a record only: "García Márquez" and "Garcia Marquez", "Jo Nesbø" and
    "Jo Nesbo" compare equal (#743)."""
    if not isinstance(value, str):
        return value
    decomposed = unicodedata.normalize("NFKD", value.translate(_FOLD_LETTERS))
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
