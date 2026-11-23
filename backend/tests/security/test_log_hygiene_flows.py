"""Log hygiene (#692): real flows through the app leave no email, allowlist
value, token, Google ``sub`` or Authorization header in any Python log record
or Sentry event/transaction.

Database-free: a small fake session stands in for Postgres, the Google
verifier and AI provider are faked at their boundaries, and JWTs are real
(a throwaway RSA key), so the real ``Authorization`` handling runs.

Flows covered today: Google sign-in, refresh, logout, sign out of all
devices (logout-all) and the revoked-token rejections after it (#689),
refresh-token reuse detection and its Sentry alert (#690),
library add (wishlist), delete (to the trash), the trash list and restore
(#683), a scan, the account export request and its delivery job (#694),
the allowlist-reject path, and a server error. Account
deletion does not exist yet: the story that adds it must drive its flow inside
``capture()`` here (see ``tests/_log_hygiene.py``).

Run on its own with:  pytest -m security tests/security/test_log_hygiene_flows.py
"""

import asyncio
import io
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
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
from tests._idempotency_fake import install_fake_store
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
            result.scalar_one.return_value = self.user_book
            result.scalars.return_value.all.return_value = (
                [self.user_book] if self.user_book else []
            )
        return result

    async def get(self, model, pk, **_options):
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

    async def refresh(self, obj, attrs=None, **_options):
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

    def test_logout_all_and_revoked_tokens(self, db):
        client = TestClient(app)
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            access, refresh = tokens["access_token"], tokens["refresh_token"]
            auth = {"Authorization": f"Bearer {access}"}
            out = client.post("/auth/logout-all", headers=auth)
            assert out.status_code == 204, out.text
            # The fake session does not run the UPDATE: bump by hand.
            db.user.token_version += 1
            me = client.get("/auth/me", headers=auth)
            assert me.status_code == 401
            assert me.json()["detail"] == "token_revoked"
            again = client.post("/auth/refresh", json={"refresh_token": refresh})
            assert again.status_code == 401
            assert again.json()["detail"] == "token_revoked"
        _clean(cap, tokens=[access, refresh])

    def test_refresh_token_reuse(self, db):
        """#690: a rotated refresh token replayed after the grace window
        revokes every session and sends a Sentry warning; neither the log nor
        the event carries a token, jti, email or Google sub."""
        client = TestClient(app)
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            access, refresh = tokens["access_token"], tokens["refresh_token"]
            rotated = client.post("/auth/refresh", json={"refresh_token": refresh})
            assert rotated.status_code == 200, rotated.text
            # Rotated longer ago than the grace window.
            old = db.tokens[0]
            old.used_at -= timedelta(seconds=settings.refresh_reuse_grace_seconds + 1)
            reused = client.post("/auth/refresh", json={"refresh_token": refresh})
            assert reused.status_code == 401
            assert reused.json()["detail"] == "refresh_reused"
        alerts = [p for p in cap.sentry_payloads if p.get("level") == "warning"]
        assert len(alerts) == 1
        assert any("reuse detected" in t for t in cap.log_texts)
        _clean(
            cap,
            tokens=[
                access,
                refresh,
                rotated.json()["access_token"],
                rotated.json()["refresh_token"],
                *(t.jti for t in db.tokens),
            ],
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

    def test_google_audience_mismatch_logs_no_client_ids(self, db):
        """#988: the aud-mismatch diagnostic logs fingerprints, never raw IDs."""
        from authlib.jose.errors import InvalidClaimError

        from app.auth.google import verify_google_id_token

        bad_aud = "leaky-token-aud-988.apps.googleusercontent.com"
        web_id = "leaky-web-id-988.apps.googleusercontent.com"
        ios_id = "leaky-ios-id-988.apps.googleusercontent.com"

        class _Claims(dict):
            def validate(self):
                raise InvalidClaimError("aud")

        with (
            lh.capture() as cap,
            patch(
                "app.auth.google.google_jwks_cache.get_jwks", AsyncMock(return_value={})
            ),
            patch(
                "app.auth.google.authlib_jwt.decode", return_value=_Claims(aud=bad_aud)
            ),
            patch.object(settings, "google_client_id", web_id),
            patch.object(settings, "google_ios_client_id", ios_id),
            patch.object(settings, "google_android_client_id", ""),
            pytest.raises(InvalidClaimError),
        ):
            asyncio.run(verify_google_id_token(ID_TOKEN))
        assert any("audience not allowed" in t for t in cap.log_texts)
        lh.assert_clean(
            cap, emails=[EMAIL], secrets=[SUB, ID_TOKEN, bad_aud, web_id, ios_id]
        )

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

    def test_delete_trash_and_restore(self, db):
        """#683: moving an entry to the trash, listing the trash and
        restoring it log no token or email."""
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
            entry = added.json()["id"]
            deleted = client.delete(f"/user-books/{entry}", headers=auth)
            assert deleted.status_code == 204, deleted.text
            assert db.user_book.deleted_at is not None
            listed = client.get("/user-books/trash", headers=auth)
            assert listed.status_code == 200, listed.text
            assert [e["id"] for e in listed.json()] == [entry]
            restored = client.post(f"/user-books/{entry}/restore", headers=auth)
            assert restored.status_code == 200, restored.text
            assert db.user_book.deleted_at is None
        _clean(cap, tokens=[tokens["access_token"], tokens["refresh_token"]])

    def test_stale_and_missing_if_match(self, db):
        """#685: a refused stale write (412) and an accepted one without
        If-Match log the user id only, never a token or email."""
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
            path = f"/user-books/{added.json()['id']}"
            stale = client.delete(path, headers={**auth, "If-Match": '"9"'})
            assert stale.status_code == 412, stale.text
            deleted = client.delete(path, headers=auth)
            assert deleted.status_code == 204, deleted.text
        _clean(cap, tokens=[tokens["access_token"], tokens["refresh_token"]])

    def test_idempotency_keys_and_stored_bodies_stay_out_of_logs(self, db, monkeypatch):
        """#685: a keyed add and edit, their replays, a reused key, a
        malformed key and a missing one log the user id only: never a key,
        the stored body (here, private notes) or a token."""
        install_fake_store(monkeypatch)
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
        notes = "Private: lent to my sister, idempotency leak probe"
        add_key, edit_key = "idem-key-685-add-4b1e", "idem-key-685-edit-9c2f"
        wishlist = {
            "open_library_work_id": WORK,
            "title": "Dune",
            "author": "Frank Herbert",
            "editions": [],
        }
        with lh.capture() as cap:
            tokens = _sign_in(client).json()
            auth = {"Authorization": f"Bearer {tokens['access_token']}"}
            with patch(
                "app.services.wishlist.WishlistService._find_or_create_edition",
                AsyncMock(return_value=None),
            ):
                for _ in range(2):
                    added = client.post(
                        "/wishlist",
                        headers={**auth, "Idempotency-Key": add_key},
                        json=wishlist,
                    )
                    assert added.status_code == 201, added.text
            assert added.headers["Idempotent-Replayed"] == "true"
            path = f"/user-books/{added.json()['id']}"
            edit = {**auth, "Idempotency-Key": edit_key}
            for _ in range(2):
                patched = client.patch(path, json={"notes": notes}, headers=edit)
                assert patched.status_code == 200, patched.text
            assert patched.headers["Idempotent-Replayed"] == "true"
            reused = client.patch(path, json={"notes": "other"}, headers=edit)
            assert reused.status_code == 422
            bad = client.patch(path, json={}, headers={**auth, "Idempotency-Key": ""})
            assert bad.status_code == 400
            assert client.patch(path, json={"rating": 3}, headers=auth).is_success
        logs = "\n".join(cap.log_texts)
        assert "idempotency: replayed" in logs
        # The stored body (the replayed notes) is never logged. (Sentry's own
        # request-body capture of the PATCH is not this flow's doing.)
        assert notes not in logs
        assert '"notes"' not in logs
        # The key is in no log and, as a scrubbed header, in no Sentry event.
        _clean(
            cap,
            tokens=[tokens["access_token"], tokens["refresh_token"], add_key, edit_key],
        )

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


class TestAiConsentFlow:
    def test_grant_and_withdraw(self, db):
        """#1023: giving and withdrawing consent logs the user id only."""
        client = TestClient(app)
        with lh.capture() as cap:
            access = _sign_in(client).json()["access_token"]
            auth = {"Authorization": f"Bearer {access}"}
            assert client.post("/users/me/ai-consent", headers=auth).status_code == 200
            assert (
                client.delete("/users/me/ai-consent", headers=auth).status_code == 200
            )
        assert db.user.ai_consent_at is not None
        assert db.user.ai_consent is False
        assert "AI consent withdrawn" in "\n".join(cap.log_texts)
        _clean(cap, tokens=[access])


class _ExportDb(FakeDb):
    """``FakeDb`` plus what the account export (#694) reads and writes: the
    cap lookup (``scalar``), the export row (``get`` / ``add``) and the
    worker's fenced updates of it (applied to the row)."""

    def __init__(self) -> None:
        super().__init__()
        self.export = None

    async def scalar(self, _stmt):
        return None  # no export in the last 24 hours

    async def get(self, model, pk, **_options):
        from app.models.account_export import AccountExport

        if model is AccountExport:
            return self.export
        return await super().get(model, pk)

    def add(self, obj):
        from app.models.account_export import AccountExport

        if isinstance(obj, AccountExport):
            self.export = obj
        else:
            super().add(obj)

    async def execute(self, stmt):
        from sqlalchemy.sql.dml import Update

        if isinstance(stmt, Update) and self.export is not None:
            for column, bind in stmt._values.items():
                setattr(self.export, column.key, bind.value)
        return await super().execute(stmt)


class TestAccountExportFlow:
    def test_request_and_delivery_log_ids_only(self, monkeypatch):
        """#694: queueing an export and the job that uploads it, signs the
        link and emails it never log the address, the link, its token or
        the storage and email keys."""
        import httpx

        from app.core.database import get_db
        from app.services import _http_retry, account_export_jobs
        from app.services.object_storage import ObjectStorage

        db = _ExportDb()

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db
        monkeypatch.setattr(_http_retry, "BACKOFF_S", 0)
        service_key = f"svc-{uuid.uuid5(uuid.NAMESPACE_URL, 'hygiene-storage')}"
        resend_key = f"rs-{uuid.uuid5(uuid.NAMESPACE_URL, 'hygiene-resend')}"
        token = f"tok-{uuid.uuid5(uuid.NAMESPACE_URL, 'hygiene-link')}"
        sent = []

        def supabase(request):
            if "/object/sign/" in request.url.path:
                return httpx.Response(
                    200, json={"signedURL": f"/object/sign/b/x.json?token={token}"}
                )
            return httpx.Response(200, json={})

        def resend(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "e"})

        storage = ObjectStorage(
            "https://ref.supabase.co",
            service_key,
            "account-exports",
            transport=httpx.MockTransport(supabase),
        )

        class _Sessions:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *exc):
                return False

        client = TestClient(app)
        try:
            with (
                lh.capture() as cap,
                patch.multiple(
                    settings,
                    supabase_url="https://ref.supabase.co",
                    supabase_service_role_key=service_key,
                    resend_api_key=resend_key,
                ),
            ):
                tokens = _sign_in(client).json()
                auth = {"Authorization": f"Bearer {tokens['access_token']}"}
                resp = client.post("/account/export", headers=auth)
                assert resp.status_code == 202, resp.text
                # As claim_next leaves it.
                db.export.status, db.export.claimed_at = "running", NOW
                delivered = asyncio.run(
                    account_export_jobs.process(
                        account_export_jobs.Claim(db.export.id, NOW),
                        storage,
                        sessions=_Sessions,
                        email_transport=httpx.MockTransport(resend),
                    )
                )
        finally:
            app.dependency_overrides.clear()
        assert delivered and db.export.status == "ready"
        (message,) = sent
        assert message["to"] == [EMAIL] and token in message["text"]
        assert "Account export queued" in "\n".join(cap.log_texts)
        _clean(
            cap,
            tokens=[
                tokens["access_token"],
                tokens["refresh_token"],
                token,
                service_key,
                resend_key,
            ],
        )


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
            patch("app.ai.gateway.AnthropicVisionProvider") as provider_cls,
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
