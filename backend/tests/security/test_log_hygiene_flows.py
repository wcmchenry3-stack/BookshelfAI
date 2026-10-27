"""Log hygiene (#692): real flows through the app leave no email, allowlist
value, token, Google ``sub`` or Authorization header in any Python log record
or Sentry event/transaction.

Database-free: a small fake session stands in for Postgres, the Google
verifier and AI provider are faked at their boundaries, and JWTs are real
(a throwaway RSA key), so the real ``Authorization`` handling runs.

Flows covered today: Google sign-in, refresh, logout, library add (wishlist)
and delete, a scan, the allowlist-reject path, and a server error. Account
deletion does not exist yet: the story that adds it must drive its flow inside
``capture()`` here (see ``tests/_log_hygiene.py``).

Run on its own with:  pytest -m security tests/security/test_log_hygiene_flows.py
"""

import io
import json
import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from PIL import Image

from app.ai.providers.base import ProviderResult
from app.core.config import settings
from app.main import app
from app.models.book import Book
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.user_book import UserBook
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate
from tests import _log_hygiene as lh
from tests.unit._quota_fake import FakeQuotaStore
from tests.unit._scan_records_fake import FakeScanRecords

pytestmark = pytest.mark.security

EMAIL = "reader.hygiene@leaktest.example"
ALLOWLISTED = "owner.allowed@leaktest.example"
SUB = "google-sub-692-4f9c1d"
ID_TOKEN = "google-id-token-692-opaque-value"
NOW = datetime.now(UTC)
WORK = "OL45804W"


@pytest.fixture(scope="module")
def _keys():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        .decode()
    )
    return private, public


@pytest.fixture(autouse=True)
def _jwt_settings(_keys):
    with (
        patch.object(settings, "jwt_private_key", _keys[0]),
        patch.object(settings, "jwt_public_key", _keys[1]),
    ):
        yield


class FakeDb:
    """Just enough of an AsyncSession for the auth, wishlist and library
    delete flows. Rows live in plain attributes."""

    def __init__(self) -> None:
        self.user: User | None = None
        self.tokens: list[RefreshToken] = []
        self.book: Book | None = None
        self.user_book: UserBook | None = None
        self.fail_commit = False

    async def execute(self, stmt):
        result = MagicMock()
        result.all.return_value = []
        result.scalar_one_or_none.return_value = None
        result.scalar_one.return_value = 0
        result.rowcount = 1
        if not getattr(stmt, "is_select", False):
            return result  # Insert / Update / Delete: accepted, nothing stored
        entity = stmt.column_descriptions[0]["entity"]
        if entity is User:
            result.scalar_one_or_none.return_value = self.user
        elif entity is RefreshToken:
            params = {str(v) for v in stmt.compile().params.values()}
            result.scalar_one_or_none.return_value = next(
                (t for t in reversed(self.tokens) if t.jti in params), None
            )
        elif entity is Book:
            result.scalar_one_or_none.return_value = self.book
        elif entity is UserBook:
            result.scalar_one_or_none.return_value = self.user_book
        return result

    async def get(self, model, pk):
        return self.user if model is User else None

    def add(self, obj):
        if isinstance(obj, User):
            obj.id = obj.id or uuid.uuid4()
            self.user = obj
        elif isinstance(obj, RefreshToken):
            self.tokens.append(obj)
        elif isinstance(obj, UserBook):
            self.user_book = obj

    async def delete(self, obj):
        self.user_book = None

    async def flush(self):
        pass

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("database exploded")

    async def rollback(self):
        pass

    async def refresh(self, obj, attrs=None):
        if isinstance(obj, UserBook):
            obj.id = obj.id or uuid.uuid4()
            obj.created_at = obj.updated_at = NOW
            obj.book, obj.edition = self.book, None
        elif isinstance(obj, Book):
            obj.editions = []
        elif isinstance(obj, User):
            obj.created_at = obj.updated_at = NOW

    def begin_nested(self):
        db = self

        class _Ctx:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def db():
    from app.core.database import get_db

    fake = FakeDb()

    async def _db():
        yield fake

    app.dependency_overrides[get_db] = _db
    yield fake
    app.dependency_overrides.clear()


def _claims(email=EMAIL):
    return {
        "sub": SUB,
        "email": email,
        "email_verified": True,
        "name": "Hygiene Reader",
        "picture": "https://example.com/a.jpg",
    }


def _sign_in(client, claims=None):
    with patch(
        "app.api.auth.verify_google_id_token",
        AsyncMock(return_value=claims or _claims()),
    ):
        return client.post("/auth/google", json={"id_token": ID_TOKEN})


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (5, 8, 7)).save(buf, format="JPEG")
    return buf.getvalue()


def _clean(captured, *, tokens=(), emails=(EMAIL,), allowlist=()):
    assert captured.log_texts, "the flow logged nothing: capture is not working"
    lh.assert_clean(
        captured,
        emails=emails,
        allowlist=allowlist,
        secrets=[SUB, ID_TOKEN, *tokens],
    )


class TestAuthFlows:
    def test_sign_in_refresh_logout_and_authorized_call(self, db):
        client = TestClient(app)
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            access, refresh = tokens["access_token"], tokens["refresh_token"]
            rotated = client.post("/auth/refresh", json={"refresh_token": refresh})
            assert rotated.status_code == 200, rotated.text
            new_refresh = rotated.json()["refresh_token"]
            me = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
            assert me.status_code == 200
            out = client.post("/auth/logout", json={"refresh_token": new_refresh})
            assert out.status_code == 204
        assert db.user is not None and db.user.email == EMAIL
        _clean(
            cap, tokens=[access, refresh, new_refresh, rotated.json()["access_token"]]
        )

    def test_failed_sign_in_and_bad_tokens(self, db):
        client = TestClient(app)
        with lh.capture() as cap:
            with patch(
                "app.api.auth.verify_google_id_token",
                AsyncMock(side_effect=Exception("signature mismatch")),
            ):
                assert (
                    client.post("/auth/google", json={"id_token": ID_TOKEN}).status_code
                    == 401
                )
            client.post("/auth/refresh", json={"refresh_token": "not.a.jwt"})
            client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
        # The verifier's own exception text is the verifier's; the app's log
        # lines must not echo the ID token or email.
        lh.assert_clean(cap, emails=[EMAIL], secrets=[SUB, ID_TOKEN])

    def test_allowlist_reject_path(self, db):
        client = TestClient(app)
        with (
            patch.object(settings, "allowed_emails", ALLOWLISTED),
            lh.capture() as cap,
        ):
            resp = _sign_in(client)
        assert resp.status_code == 403
        _clean(cap, allowlist=[ALLOWLISTED])

    def test_allowlist_accept_path(self, db):
        client = TestClient(app)
        with (
            patch.object(settings, "allowed_emails", f"{EMAIL},{ALLOWLISTED}"),
            lh.capture() as cap,
        ):
            tokens = _sign_in(client).json()
        _clean(
            cap,
            allowlist=[ALLOWLISTED],
            tokens=[tokens["access_token"], tokens["refresh_token"]],
        )


class TestLibraryFlows:
    def test_add_and_delete(self, db):
        client = TestClient(app)
        db.book = Book(
            id=uuid.uuid4(),
            open_library_work_id=WORK,
            title="Dune",
            author="Frank Herbert",
            language="en",
            created_at=NOW,
            updated_at=NOW,
        )
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            auth = {"Authorization": f"Bearer {tokens['access_token']}"}
            with patch(
                "app.services.wishlist.WishlistService._find_or_create_edition",
                AsyncMock(return_value=None),
            ):
                added = client.post(
                    "/wishlist",
                    headers=auth,
                    json={
                        "open_library_work_id": WORK,
                        "title": "Dune",
                        "author": "Frank Herbert",
                        "editions": [],
                    },
                )
            assert added.status_code == 201, added.text
            deleted = client.delete(f"/user-books/{added.json()['id']}", headers=auth)
            assert deleted.status_code == 204, deleted.text
        _clean(cap, tokens=[tokens["access_token"], tokens["refresh_token"]])

    def test_server_error_reaches_sentry_without_pii(self, db):
        client = TestClient(app, raise_server_exceptions=False)
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            db.fail_commit = True
            db.user_book = UserBook(
                id=uuid.uuid4(), user_id=db.user.id, book_id=uuid.uuid4()
            )
            db.user_book.custom_book = None
            resp = client.delete(
                f"/user-books/{db.user_book.id}",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
        assert resp.status_code == 500
        errors = [
            p
            for p in cap.sentry_payloads
            if p.get("exception") and "/user-books/" in json.dumps(p, default=str)
        ]
        assert errors, "no Sentry exception event captured for the failing request"
        _clean(cap, tokens=[tokens["access_token"], tokens["refresh_token"]])


class TestScanFlow:
    def test_scan_with_faked_provider(self, db):
        client = TestClient(app)
        store, records = FakeQuotaStore(), FakeScanRecords()
        image = _jpeg()

        async def _enrich(candidates, limit):
            enrich.return_value.last_complete = True
            return [
                EnrichedBook(
                    open_library_work_id=WORK,
                    title="Dune",
                    author="Frank Herbert",
                    confidence=0.9,
                    editions=[],
                )
                for _ in candidates
            ]

        with (
            lh.capture() as cap,
            store.installed(),
            records.installed(),
            patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
            patch("app.api.scan.EnrichmentService") as enrich,
            patch("app.api.scan.ledger.record", AsyncMock()),
        ):
            provider_cls.return_value.identify = AsyncMock(
                return_value=ProviderResult(
                    [BookCandidate(title="Dune", author="F", confidence=0.9)]
                )
            )
            enrich.return_value.enrich = AsyncMock(side_effect=_enrich)
            tokens = _sign_in(client).json()
            store.default_user = db.user.id
            resp = client.post(
                "/scan",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
                files={"file": ("s.jpg", io.BytesIO(image), "image/jpeg")},
            )
        assert resp.status_code == 200, resp.text
        assert cap.error_texts == [], "the scan flow logged ERROR records"
        _clean(cap, tokens=[tokens["access_token"], tokens["refresh_token"]])


class TestCaptureCatchesALeak:
    """Proof the flows above would fail: a real logger emitting an email (or a
    token) inside the capture is flagged, even though the app's redaction
    filter would later scrub it."""

    def test_email_logged_through_a_real_logger_is_flagged(self):
        with lh.capture() as cap:
            logging.getLogger("app.api.auth").info("signed in %s", EMAIL)
        findings = lh.check(cap, emails=[EMAIL])
        assert {f.kind for f in findings} >= {"email"}
        assert all(f.source == "log" for f in findings)
        with pytest.raises(AssertionError, match="email found in log"):
            lh.assert_clean(cap, emails=[EMAIL])

    def test_leak_in_sentry_is_flagged(self):
        import sentry_sdk

        with lh.capture() as cap:
            logging.getLogger("app.api.auth").error("auth failed for %s", EMAIL)
            sentry_sdk.capture_message(f"hello {EMAIL}")
        sources = {f.source for f in lh.check(cap, emails=[EMAIL])}
        assert "sentry" in sources
