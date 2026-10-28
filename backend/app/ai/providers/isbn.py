"""ISBN cleaning shared by the vision adapters (#746).

A vision model can invent an ISBN: it looks right but fails the check digit.
Such a value wastes provider lookups and can point at the wrong book, so both
adapters drop it (the book itself is kept).
"""

import re

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
