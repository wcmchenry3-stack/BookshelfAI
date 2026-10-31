"""ISBN validation and conversion, and the ISBNs a provider record lists."""


def _gb_isbns(volume: dict | None) -> set[str]:
    info = (volume or {}).get("volumeInfo") or {}
    identifiers = info.get("industryIdentifiers") or []
    if not isinstance(identifiers, list):
        return set()
    return {
        entry["identifier"]
        for entry in identifiers
        if isinstance(entry, dict) and isinstance(entry.get("identifier"), str)
    }


def _ol_isbns(doc: dict | None) -> set[str]:
    values = (doc or {}).get("isbn") or []
    return (
        {v for v in values if isinstance(v, str)} if isinstance(values, list) else set()
    )


def isbn_13_to_10(isbn_13: str | None) -> str | None:
    """The ISBN-10 form of a 978-prefixed ISBN-13, else None."""
    if not isbn_13 or len(isbn_13) != 13 or not isbn_13.isdigit():
        return None
    if not isbn_13.startswith("978"):
        return None
    body = isbn_13[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(body))
    check = (11 - total % 11) % 11
    return body + ("X" if check == 10 else str(check))


def isbn_10_to_13(isbn_10: str | None) -> str | None:
    """The 978-prefixed ISBN-13 form of an ISBN-10, else None."""
    if not isinstance(isbn_10, str):
        return None
    value = isbn_10.replace("-", "").replace(" ", "").upper()
    if len(value) != 10 or not value[:9].isdigit():
        return None
    if not (value[9].isdigit() or value[9] == "X"):
        return None
    body = "978" + value[:9]
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


def normalise_isbn_13(isbn_13: str | None, isbn_10: str | None = None) -> str | None:
    """One comparable key for an edition: its ISBN-13, else its ISBN-10 as
    an ISBN-13 (#587). None when it has neither."""
    if isinstance(isbn_13, str):
        value = isbn_13.replace("-", "").replace(" ", "")
        if len(value) == 13 and value.isdigit():
            return value
    return isbn_10_to_13(isbn_10)


def _edition_isbns(
    isbn_13: str | None, isbn_10: str | None, confirmed: set[str]
) -> tuple[str | None, str | None]:
    """The candidate's ISBNs a provider confirmed, paired consistently: an
    ISBN-10 beside a confirmed ISBN-13 only when it is that ISBN-13's
    ISBN-10 form; without one, a confirmed ISBN-10 on its own."""
    # Never an invalid ISBN-13, even one a provider happens to list.
    isbn_13 = isbn_13 if isbn_13 in confirmed and valid_isbn_13(isbn_13) else None
    if isbn_10 is not None:
        isbn_10 = isbn_10.upper()
    if isbn_13:
        return isbn_13, isbn_10 if isbn_10 == isbn_13_to_10(isbn_13) else None
    confirmed_upper = {c.upper() for c in confirmed}
    return None, isbn_10 if isbn_10 in confirmed_upper else None


def _isbn_digits(value: object) -> object:
    """An ISBN without hyphens and spaces (anything else unchanged)."""
    if not isinstance(value, str):
        return value
    return value.replace("-", "").replace(" ", "")


def valid_isbn_13(value: object) -> bool:
    """Whether ``value`` is 13 ASCII digits with a valid check digit."""
    if (
        not isinstance(value, str)
        or len(value) != 13
        or not value.isascii()
        or not value.isdigit()
    ):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(value))
    return total % 10 == 0
