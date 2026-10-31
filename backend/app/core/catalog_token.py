"""Server-signed catalog tokens (#602).

The shared catalog (``books``, ``editions``) is written only from data the
providers (Open Library / Google Books) returned to the server. The app adds
a book to the wishlist by posting back an ``EnrichedBook`` it got from
``POST /scan`` or ``GET /books/search``, so the server signs such a result:
``catalog_token`` is an HMAC-SHA256 over

- a canonical serialisation of the catalog fields (:data:`BOOK_FIELDS`,
  ``subjects`` and, per edition, :data:`EDITION_FIELDS`): JSON with sorted
  keys and no whitespace;
- the requesting user's ID;
- the expiry and the token version.

What a valid token guarantees: the server produced exactly these field
values for this user from provider records, within the TTL. It does not
stop the user from choosing *which* provider record a photo or query
matches. The enrichment service (``app/services/enrichment.py``) is what
keeps user-controlled input out of the signed fields: whenever a provider
matched, the title, author, IDs, description, cover, subjects and edition
details come from the provider records, and an edition ISBN read only by the
vision model is signed only when a provider confirmed it. A result with no
provider ID (nothing matched, so its title and author are the model's
reading of the photo) is never signed (:func:`sign_books`), and the wishlist
service never creates a row for one.

``POST /wishlist`` recomputes the MAC over what it receives and the
authenticated user. Only a valid token lets it create catalog rows; anything
else may only link the user to rows that already exist.

Token format (opaque to the client)::

    base64url(json({"exp": <unix seconds>, "v": "v1"})) "." base64url(mac)

The expiry is the end of the UTC day the token was issued plus
:data:`CATALOG_TOKEN_TTL` (30 days, the scan-record retention, because a
result can sit in the app's queue). Day granularity makes the token for the
same result, user and day identical, so a repeat served from a scan record
returns exactly what the first scan did.

Key: HMAC-SHA256(``JWT_PRIVATE_KEY``, ``"bookshelf-catalog-token-v1"``). The
JWT key is the one server secret every instance shares (Render runs two), so
a token issued by one instance verifies on the other. When it is not set,
hardened environments (staging, production) refuse to boot
(:func:`check_configured`, called from the app's startup checks) and refuse
to sign or verify; development and test use :data:`DEV_ONLY_SECRET`.

Never log the key or a token.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.core.config import Settings

CATALOG_TOKEN_VERSION = "v1"
CATALOG_TOKEN_TTL = timedelta(days=30)
logger = logging.getLogger(__name__)

# Real tokens are about 80 characters; the request schema bounds the field.
CATALOG_TOKEN_MAX_LENGTH = 256
# Key-derivation label; a new token version gets a new label.
KEY_LABEL = b"bookshelf-catalog-token-v1"
# Development / test only: hardened environments never fall back to it.
DEV_ONLY_SECRET = "bookshelf-DEV-ONLY-catalog-token-secret-never-used-in-production"

BOOK_FIELDS = (
    "open_library_work_id",
    "google_books_id",
    "title",
    "author",
    "description",
    "cover_url",
)
EDITION_FIELDS = (
    "isbn_13",
    "isbn_10",
    "publisher",
    "publish_year",
    "format",
    "page_count",
    "open_library_edition_id",
)

_DAY_SECONDS = 24 * 60 * 60

# Why a token did not verify (never the token itself); "ok" when it did.
OK = "ok"
MISSING = "missing"
MALFORMED = "malformed"
WRONG_VERSION = "wrong_version"
EXPIRED = "expired"
MISMATCH = "mismatch"


class CatalogTokenConfigError(RuntimeError):
    """No secret to derive the catalog-token key from in a hardened env."""


class SignableEdition(Protocol):
    isbn_13: str | None
    isbn_10: str | None
    publisher: str | None
    publish_year: int | None
    format: str | None
    page_count: int | None
    open_library_edition_id: str | None


class SignableBook(Protocol):
    """An ``EnrichedBook`` (signed) or a ``WishlistRequest`` (verified)."""

    open_library_work_id: str | None
    google_books_id: str | None
    title: str
    author: str
    description: str | None
    cover_url: str | None
    subjects: list[str]
    editions: list[SignableEdition]


# ---------------------------------------------------------------------------
# Key
# ---------------------------------------------------------------------------


def derive_key(secret: str) -> bytes:
    """The HMAC key for ``secret``: HMAC-SHA256(secret, :data:`KEY_LABEL`)."""
    return hmac.new(secret.encode("utf-8"), KEY_LABEL, hashlib.sha256).digest()


def _settings() -> Settings:
    # Imported on use, so the request schemas can import this module's
    # constants without loading the settings.
    from app.core.config import settings

    return settings


def _secret(cfg: Settings) -> str:
    secret = (cfg.jwt_private_key or "").strip()
    if secret:
        return secret
    if cfg.is_hardened:
        raise CatalogTokenConfigError(
            "JWT_PRIVATE_KEY must be configured: the catalog-token key (#602) "
            f"is derived from it, and ENVIRONMENT={cfg.environment!r} never "
            "uses the development key."
        )
    return DEV_ONLY_SECRET


def check_configured(cfg: Settings | None = None) -> None:
    """Raise :class:`CatalogTokenConfigError` when no key can be derived
    (a hardened environment without ``JWT_PRIVATE_KEY``)."""
    _secret(cfg or _settings())


@lru_cache(maxsize=4)
def _cached_key(secret: str) -> bytes:
    return derive_key(secret)


def _key() -> bytes:
    return _cached_key(_secret(_settings()))


# ---------------------------------------------------------------------------
# Canonical form and MAC
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def canonical_book(book: SignableBook) -> dict[str, Any]:
    """The catalog-relevant fields of ``book``, as signed."""
    data: dict[str, Any] = {name: getattr(book, name) for name in BOOK_FIELDS}
    data["subjects"] = list(book.subjects or [])
    data["editions"] = [
        {name: getattr(edition, name) for name in EDITION_FIELDS}
        for edition in (book.editions or [])
    ]
    return data


def _mac(
    key: bytes, *, version: str, exp: int, user_id: uuid.UUID, book: SignableBook
) -> bytes:
    message = _dumps(
        {
            "book": canonical_book(book),
            "exp": exp,
            "user": str(user_id),
            "v": version,
        }
    )
    return hmac.new(key, message, hashlib.sha256).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


_B64URL_ALPHABET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _b64decode(text: str) -> bytes:
    """Strict unpadded base64url: any character outside ``[A-Za-z0-9_-]``
    (padding included) is :class:`binascii.Error` rather than skipped, so
    exactly one string decodes to a given payload."""
    if not set(text) <= _B64URL_ALPHABET:
        raise binascii.Error("not base64url")
    return base64.b64decode(
        text + "=" * (-len(text) % 4), altchars=b"-_", validate=True
    )


def expiry_for(now: datetime) -> int:
    """Unix seconds: the end of ``now``'s UTC day plus the TTL."""
    day_end = (int(now.timestamp()) // _DAY_SECONDS + 1) * _DAY_SECONDS
    return day_end + int(CATALOG_TOKEN_TTL.total_seconds())


def _encode(*, version: str, exp: int, user_id: uuid.UUID, book: SignableBook) -> str:
    payload = _b64encode(_dumps({"exp": exp, "v": version}))
    mac = _mac(_key(), version=version, exp=exp, user_id=user_id, book=book)
    return f"{payload}.{_b64encode(mac)}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sign(book: SignableBook, user_id: uuid.UUID, now: datetime | None = None) -> str:
    """A token binding ``book``'s catalog fields to ``user_id``."""
    exp = expiry_for(now or datetime.now(UTC))
    return _encode(version=CATALOG_TOKEN_VERSION, exp=exp, user_id=user_id, book=book)


def is_signable(book: SignableBook) -> bool:
    """Only a book a provider matched (it has a provider ID) may ever create
    a catalog row; one with neither ID is the model's reading alone."""
    return bool(book.open_library_work_id or book.google_books_id)


def sign_books(
    books: list[Any], user_id: uuid.UUID, now: datetime | None = None
) -> list[Any]:
    """Set ``catalog_token`` on every signable ``EnrichedBook`` in ``books``
    (in place) and return the list. A book with no provider ID
    (:func:`is_signable`) gets ``None``. Never fails the response: a book
    that cannot be signed gets no token (it can then only be linked to an
    existing catalog row, never create one)."""
    when = now or datetime.now(UTC)
    for book in books:
        if not is_signable(book):
            book.catalog_token = None
            continue
        try:
            book.catalog_token = sign(book, user_id, when)
        except Exception as exc:  # noqa: BLE001 - fail closed, not the scan
            logger.warning("catalog_token: could not sign (%s)", type(exc).__name__)
            book.catalog_token = None
    return books


def check(
    token: str | None,
    book: SignableBook,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> str:
    """:data:`OK`, or why ``token`` does not vouch for ``book`` for
    ``user_id`` (:data:`MISSING`, :data:`MALFORMED`, :data:`WRONG_VERSION`,
    :data:`EXPIRED`, :data:`MISMATCH`)."""
    if not token:
        return MISSING
    if not isinstance(token, str) or len(token) > CATALOG_TOKEN_MAX_LENGTH:
        return MALFORMED
    parts = token.split(".")
    if len(parts) != 2:
        return MALFORMED
    try:
        payload = json.loads(_b64decode(parts[0]))
        given_mac = _b64decode(parts[1])
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return MALFORMED
    if not isinstance(payload, dict):
        return MALFORMED
    version, exp = payload.get("v"), payload.get("exp")
    if (
        not isinstance(version, str)
        or isinstance(exp, bool)
        or not isinstance(exp, int)
    ):
        return MALFORMED
    if version != CATALOG_TOKEN_VERSION:
        return WRONG_VERSION
    expected = _mac(_key(), version=version, exp=exp, user_id=user_id, book=book)
    if not hmac.compare_digest(given_mac, expected):
        return MISMATCH
    if exp <= int((now or datetime.now(UTC)).timestamp()):
        return EXPIRED
    return OK
