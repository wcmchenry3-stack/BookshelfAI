"""Catalog tokens (#602): the server's signature over a scan / search result.

Database-free: signing and verifying, which changes invalidate a token, the
key derivation, and the hardened-environment fail-closed rule.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.ai import scan_records
from app.core import catalog_token
from app.core.config import Settings
from app.schemas.book import EditionPreview, EnrichedBook
from app.schemas.user_book import WishlistRequest

ALICE = uuid.UUID("00000000-0000-0000-0000-0000000602a1")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000602b2")
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

EDITION = EditionPreview(
    isbn_13="9780441013593",
    isbn_10="0441013597",
    publisher="Ace",
    publish_year=1990,
    format="paperback",
    page_count=535,
    open_library_edition_id="OL1M",
)


def _book(**overrides) -> EnrichedBook:
    data = {
        "open_library_work_id": "OL45804W",
        "google_books_id": "gb_dune_001",
        "title": "Dune",
        "author": "Frank Herbert",
        "description": "Spice.",
        "cover_url": "https://books.google.com/dune.jpg",
        "subjects": ["Science fiction"],
        "confidence": 0.9,
        "editions": [EDITION],
    }
    data.update(overrides)
    return EnrichedBook(**data)


def _as_request(book: EnrichedBook, **overrides) -> WishlistRequest:
    """What the app posts back: the book as JSON, token included. Built
    with ``model_construct`` so no DNS lookup runs on cover_url."""
    data = book.model_dump(
        mode="json",
        exclude={"book_id", "confidence", "already_in_library", "in_library"},
    )
    data["editions"] = [EditionPreview(**e) for e in data["editions"]]
    data.update(overrides)
    return WishlistRequest.model_construct(**data)


class TestRoundTrip:
    def test_sign_then_verify(self):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        assert catalog_token.check(token, book, ALICE, NOW) == catalog_token.OK
        assert catalog_token.verify(token, book, ALICE, NOW)

    def test_verifies_the_posted_back_request(self):
        """The JSON round trip (EnrichedBook -> app -> WishlistRequest)
        keeps the canonical form."""
        book = catalog_token.sign_books([_book()], ALICE, NOW)[0]
        req = _as_request(book)
        assert catalog_token.verify(req.catalog_token, req, ALICE, NOW)

    def test_token_is_opaque_and_bounded(self):
        token = catalog_token.sign(_book(), ALICE, NOW)
        payload, mac = token.split(".")
        assert "Dune" not in token and str(ALICE) not in token
        assert len(token) <= catalog_token.CATALOG_TOKEN_MAX_LENGTH
        assert payload and mac

    def test_same_book_user_and_day_give_the_same_token(self):
        """A repeat served later the same UTC day equals the first scan."""
        later = NOW + timedelta(hours=11, minutes=59)
        assert catalog_token.sign(_book(), ALICE, NOW) == catalog_token.sign(
            _book(), ALICE, later
        )

    def test_book_without_editions_or_subjects(self):
        book = _book(editions=[], subjects=[], description=None, cover_url=None)
        token = catalog_token.sign(book, ALICE, NOW)
        assert catalog_token.verify(token, book, ALICE, NOW)


_CHANGES = [
    ("open_library_work_id", "OL1W"),
    ("open_library_work_id", None),
    ("google_books_id", "gb_other"),
    ("google_books_id", None),
    ("title", "Dune (forged)"),
    ("author", "Someone Else"),
    ("description", "<script>alert(1)</script>"),
    ("description", None),
    ("cover_url", "https://evil.example/x.jpg"),
    ("cover_url", None),
    ("subjects", ["Spam"]),
    ("subjects", []),
    ("editions", []),
    ("editions", [EDITION, EditionPreview(isbn_13="9780000000002")]),
]
_EDITION_CHANGES = [
    ("isbn_13", "9780000000002"),
    ("isbn_10", None),
    ("publisher", "Forged House"),
    ("publish_year", 1991),
    ("format", "hardcover"),
    ("page_count", 1),
    ("open_library_edition_id", "OL2M"),
]


class TestAnyChangeInvalidates:
    @pytest.mark.parametrize("field,value", _CHANGES)
    def test_book_field(self, field, value):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        assert not catalog_token.verify(token, _book(**{field: value}), ALICE, NOW)

    @pytest.mark.parametrize("field,value", _EDITION_CHANGES)
    def test_edition_field(self, field, value):
        token = catalog_token.sign(_book(), ALICE, NOW)
        changed = EDITION.model_copy(update={field: value})
        result = catalog_token.check(token, _book(editions=[changed]), ALICE, NOW)
        assert result == catalog_token.MISMATCH

    def test_every_catalog_field_is_signed(self):
        """A new catalog column must be added to the signed fields."""
        assert set(catalog_token.BOOK_FIELDS) | {"subjects", "editions"} == {
            "open_library_work_id",
            "google_books_id",
            "title",
            "author",
            "description",
            "cover_url",
            "subjects",
            "editions",
        }
        assert set(catalog_token.EDITION_FIELDS) == set(EditionPreview.model_fields)


class TestRejected:
    def test_another_users_token(self):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        assert catalog_token.check(token, book, BOB, NOW) == catalog_token.MISMATCH

    def test_expired(self):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        still = NOW + catalog_token.CATALOG_TOKEN_TTL
        assert catalog_token.verify(token, book, ALICE, still)
        after = NOW + catalog_token.CATALOG_TOKEN_TTL + timedelta(days=1)
        assert catalog_token.check(token, book, ALICE, after) == catalog_token.EXPIRED

    def test_ttl_is_thirty_days(self):
        assert catalog_token.CATALOG_TOKEN_TTL == timedelta(days=30)
        exp = catalog_token.expiry_for(NOW)
        end_of_day = datetime(2026, 10, 24, tzinfo=UTC)
        assert exp == int((end_of_day + timedelta(days=30)).timestamp())

    def test_wrong_version(self):
        """A correctly MACed token of another version is still refused."""
        book = _book()
        token = catalog_token._encode(
            version="v2",
            exp=catalog_token.expiry_for(NOW),
            user_id=ALICE,
            book=book,
        )
        assert (
            catalog_token.check(token, book, ALICE, NOW) == catalog_token.WRONG_VERSION
        )

    def test_extended_expiry_is_refused(self):
        """The expiry is under the MAC: moving it breaks the token."""
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        _, mac = token.split(".")
        payload = catalog_token._b64encode(
            catalog_token._dumps({"exp": 4_102_444_800, "v": "v1"})
        )
        forged = f"{payload}.{mac}"
        assert catalog_token.check(forged, book, ALICE, NOW) == catalog_token.MISMATCH

    @pytest.mark.parametrize("token", [None, ""])
    def test_missing(self, token):
        assert catalog_token.check(token, _book(), ALICE, NOW) == catalog_token.MISSING

    @pytest.mark.parametrize(
        "token",
        [
            "no-dot",
            "a.b.c",
            "!!!.???",
            "e30.AAAA",  # {} payload
            "WzFd.AAAA",  # [1] payload
            catalog_token._b64encode(b'{"exp":true,"v":"v1"}') + ".AAAA",
            catalog_token._b64encode(b'{"exp":1,"v":1}') + ".AAAA",
            "x" * (catalog_token.CATALOG_TOKEN_MAX_LENGTH + 1),
        ],
    )
    def test_malformed(self, token):
        assert (
            catalog_token.check(token, _book(), ALICE, NOW) == catalog_token.MALFORMED
        )


class TestKey:
    def test_same_secret_same_key(self):
        assert catalog_token.derive_key("s3cret") == catalog_token.derive_key("s3cret")
        assert len(catalog_token.derive_key("s3cret")) == 32

    def test_different_secret_different_key(self):
        assert catalog_token.derive_key("a") != catalog_token.derive_key("b")

    def test_key_is_not_the_secret(self):
        assert catalog_token.derive_key("s3cret") != b"s3cret"

    def test_tokens_from_one_secret_fail_under_another(self):
        """Every instance shares JWT_PRIVATE_KEY; a different key never
        verifies (and a random per-process key would break two instances)."""
        book = _book()
        with patch.object(
            catalog_token, "_settings", lambda: _cfg(jwt_private_key="key-one")
        ):
            token = catalog_token.sign(book, ALICE, NOW)
            assert catalog_token.verify(token, book, ALICE, NOW)
        with patch.object(
            catalog_token, "_settings", lambda: _cfg(jwt_private_key="key-one")
        ):
            assert catalog_token.verify(token, book, ALICE, NOW)
        with patch.object(
            catalog_token, "_settings", lambda: _cfg(jwt_private_key="key-two")
        ):
            assert not catalog_token.verify(token, book, ALICE, NOW)

    def test_dev_uses_the_dev_key_without_a_secret(self):
        cfg = _cfg(environment="development", jwt_private_key="")
        assert catalog_token._secret(cfg) == catalog_token.DEV_ONLY_SECRET
        catalog_token.check_configured(cfg)  # does not raise

    def test_configured_secret_is_used(self):
        cfg = _cfg(environment="production", jwt_private_key="  pem  ")
        assert catalog_token._secret(cfg) == "pem"
        catalog_token.check_configured(cfg)  # does not raise


def _cfg(**overrides) -> Settings:
    data = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "turnstile_required": False,
    }
    data.update(overrides)
    return Settings(**data)


class TestFailClosed:
    @pytest.mark.parametrize("environment", ["production", "staging"])
    def test_hardened_without_secret_raises(self, environment):
        cfg = _cfg(environment=environment, jwt_private_key="")
        with pytest.raises(catalog_token.CatalogTokenConfigError, match="JWT_PRIVATE"):
            catalog_token.check_configured(cfg)

    def test_production_without_secret_fails_at_boot(self):
        cfg = _cfg(environment="production", jwt_private_key="")

        async def _run():
            from app.main import _validate_config

            with patch("app.main.settings", cfg):
                await _validate_config()

        with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY"):
            asyncio.run(_run())

    def test_production_with_secret_boots(self):
        cfg = _cfg(environment="production", jwt_private_key="pem")

        async def _run():
            from app.main import _validate_config

            with patch("app.main.settings", cfg):
                await _validate_config()

        asyncio.run(_run())  # must not raise

    def test_hardened_without_secret_never_signs_or_verifies(self):
        cfg = _cfg(environment="production", jwt_private_key="")
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)  # dev key, test env
        with patch.object(catalog_token, "_settings", lambda: cfg):
            with pytest.raises(catalog_token.CatalogTokenConfigError):
                catalog_token.verify(token, book, ALICE, NOW)
            # A response is never failed: the book just has no token.
            (signed,) = catalog_token.sign_books([_book()], ALICE, NOW)
        assert signed.catalog_token is None

    def test_error_message_never_holds_a_secret(self):
        cfg = _cfg(environment="production", jwt_private_key="")
        with pytest.raises(catalog_token.CatalogTokenConfigError) as exc:
            catalog_token.check_configured(cfg)
        assert catalog_token.DEV_ONLY_SECRET not in str(exc.value)


class TestSignBooks:
    def test_signs_each_book_for_the_user(self):
        books = [_book(), _book(title="Children of Dune", open_library_work_id="W2")]
        out = catalog_token.sign_books(books, ALICE, NOW)
        assert out is books
        for book in books:
            assert catalog_token.verify(book.catalog_token, book, ALICE, NOW)

    def test_an_unsignable_book_gets_no_token(self):
        bad = MagicMock()  # not JSON-serialisable
        good = _book()
        catalog_token.sign_books([bad, good], ALICE, NOW)
        assert bad.catalog_token is None
        assert good.catalog_token

    def test_scan_records_never_store_the_token(self):
        """Tokens are signed per response; a stored one would outlive it."""
        (book,) = catalog_token.sign_books([_book()], ALICE, NOW)
        (item,) = scan_records.serialise_books([book])
        assert "catalog_token" not in item
        with pytest.raises(scan_records.ScanRecordError):
            scan_records.deserialise_books([{**item, "catalog_token": "x.y"}])


class TestNoProviderIdIsNeverSigned:
    """A book no provider matched (no work ID, no Google Books ID) is the
    model's reading alone: it never gets a token, so a replayed one can
    never create rows (#602 review, finding 3)."""

    def test_book_without_ids_gets_no_token(self):
        bare = _book(open_library_work_id=None, google_books_id=None)
        good = _book()
        catalog_token.sign_books([bare, good], ALICE, NOW)
        assert bare.catalog_token is None
        assert catalog_token.verify(good.catalog_token, good, ALICE, NOW)

    @pytest.mark.parametrize(
        "ids",
        [
            {"google_books_id": None},
            {"open_library_work_id": None},
        ],
        ids=["work_only", "volume_only"],
    )
    def test_one_provider_id_is_enough(self, ids):
        (book,) = catalog_token.sign_books([_book(**ids)], ALICE, NOW)
        assert catalog_token.verify(book.catalog_token, book, ALICE, NOW)


class TestStrictDecoding:
    """Base64url is decoded strictly: a character outside [A-Za-z0-9_-]
    anywhere in the token is malformed, never silently skipped (#602 review,
    finding 5)."""

    @pytest.mark.parametrize("junk", ["!", "+", "/", "=", " ", "\n", "*", "é"])
    @pytest.mark.parametrize("part", [0, 1], ids=["payload", "mac"])
    def test_injected_character_is_malformed(self, junk, part):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        assert catalog_token.verify(token, book, ALICE, NOW)
        pieces = token.split(".")
        pieces[part] = pieces[part][:4] + junk + pieces[part][4:]
        assert (
            catalog_token.check(".".join(pieces), book, ALICE, NOW)
            == catalog_token.MALFORMED
        )

    def test_padded_token_is_malformed(self):
        book = _book()
        token = catalog_token.sign(book, ALICE, NOW)
        payload, mac = token.split(".")
        padded = f"{payload}{'=' * (-len(payload) % 4)}.{mac}="
        assert catalog_token.check(padded, book, ALICE, NOW) == catalog_token.MALFORMED

    def test_unpadded_round_trip_still_decodes(self):
        for raw in (b"", b"a", b"ab", b"abc", bytes(range(256))):
            encoded = catalog_token._b64encode(raw)
            assert "=" not in encoded
            assert catalog_token._b64decode(encoded) == raw
