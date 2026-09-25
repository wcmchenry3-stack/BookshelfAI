"""Unit tests for POST /scan — services mocked, no real DB or HTTP."""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.user import User
from app.schemas.book import EditionPreview, EnrichedBook
from app.services.book_identifier import BookCandidate, ScanUnavailableError

FAKE_USER = User(id="00000000-0000-0000-0000-000000000001", email="test@example.com")

ENRICHED_BOOK = EnrichedBook(
    open_library_work_id="OL45804W",
    google_books_id="gb_dune_001",
    title="Dune",
    author="Frank Herbert",
    confidence=0.97,
    already_in_library=False,
    editions=[EditionPreview(isbn_13="9780441013593")],
)


_JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def _image_file(size_bytes: int = 100, content_type: str = "image/jpeg"):
    # Prepend valid JPEG magic bytes so the file passes magic bytes validation.
    # Pad to the requested size with neutral bytes.
    padding = b"x" * max(0, size_bytes - len(_JPEG_MAGIC))
    return ("scan.jpg", io.BytesIO(_JPEG_MAGIC + padding), content_type)


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    app.dependency_overrides[get_db] = _fake_db

    yield TestClient(app)

    app.dependency_overrides.clear()


class TestScanEndpoint:
    def test_returns_401_without_auth(self):
        client = TestClient(app)
        resp = client.post("/scan", files={"file": _image_file()})
        assert resp.status_code == 401

    def test_returns_415_for_non_image(self, client):
        resp = client.post(
            "/scan", files={"file": ("doc.pdf", io.BytesIO(b"pdf"), "application/pdf")}
        )
        assert resp.status_code == 415

    def test_returns_413_for_oversized_file(self, client):
        big = 5 * 1024 * 1024 + 1  # 1 byte over 5MB
        resp = client.post("/scan", files={"file": _image_file(size_bytes=big)})
        assert resp.status_code == 413

    def test_returns_503_when_vision_service_unavailable(self, client):
        with patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls:
            mock_id_cls.return_value.identify = AsyncMock(
                side_effect=ScanUnavailableError("timeout")
            )
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 503
        assert resp.json()["detail"] == "scan_unavailable"

    def test_returns_empty_list_when_no_candidates(self, client):
        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_id_cls.return_value.identify = AsyncMock(return_value=[])
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        assert resp.json() == {
            "books": [],
            "enhanced": False,
            "enhanced_scan_credits": 0,
        }

    def test_returns_enriched_candidates(self, client):
        candidate = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.97)

        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(return_value=[candidate])
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK]
            )
            mock_dedup_cls.return_value.check = AsyncMock(return_value=[ENRICHED_BOOK])

            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        data = resp.json()["books"]
        assert len(data) == 1
        assert data[0]["title"] == "Dune"
        assert data[0]["author"] == "Frank Herbert"
        assert data[0]["open_library_work_id"] == "OL45804W"

    def test_returns_every_book_in_a_multi_book_photo(self, client):
        candidates = [
            BookCandidate(title=f"Book {i}", author="Author", confidence=0.9)
            for i in range(4)
        ]
        books = [
            ENRICHED_BOOK.model_copy(
                update={
                    "title": f"Book {i}",
                    "open_library_work_id": f"OL{i}W",
                    "google_books_id": f"gb_{i}",
                }
            )
            for i in range(4)
        ]

        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(return_value=candidates)
            mock_enrich_cls.return_value.enrich = AsyncMock(return_value=books)
            mock_dedup_cls.return_value.check = AsyncMock(
                side_effect=lambda _db, _u, b: b
            )

            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.status_code == 200
        assert [b["title"] for b in resp.json()["books"]] == [
            "Book 0",
            "Book 1",
            "Book 2",
            "Book 3",
        ]
        # Enrichment must not be capped at the single-book limit of 3.
        _, kwargs = mock_enrich_cls.return_value.enrich.call_args
        assert kwargs["limit"] == 15
        mock_id_cls.assert_called_once_with(enhanced=False)

    def test_drops_books_that_enrich_to_the_same_work(self, client):
        dup = ENRICHED_BOOK.model_copy(update={"title": "Dune (Deluxe)"})
        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=[
                    BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
                ]
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK, dup]
            )
            mock_dedup_cls.return_value.check = AsyncMock(
                side_effect=lambda _db, _u, b: b
            )

            resp = client.post("/scan", files={"file": _image_file()})

        assert [b["title"] for b in resp.json()["books"]] == ["Dune"]


def _user_with_credits(credits: int) -> User:
    return User(
        id="00000000-0000-0000-0000-000000000002",
        email="credits@example.com",
        enhanced_scan_credits=credits,
    )


class TestEnhancedScan:
    @pytest.fixture
    def make_client(self):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        db = AsyncMock()

        def _make(user: User, remaining_after_spend: int | None = None):
            result = MagicMock()
            result.scalar_one_or_none.return_value = remaining_after_spend
            db.execute = AsyncMock(return_value=result)

            async def _fake_db():
                yield db

            app.dependency_overrides[get_current_user] = lambda: user
            app.dependency_overrides[get_db] = _fake_db
            return TestClient(app), db

        yield _make
        app.dependency_overrides.clear()

    def test_returns_402_when_out_of_credits(self, make_client):
        client, _ = make_client(_user_with_credits(0))
        with patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls:
            resp = client.post(
                "/scan", files={"file": _image_file()}, data={"enhanced": "true"}
            )
        assert resp.status_code == 402
        assert resp.json()["detail"] == "no_enhanced_credits"
        mock_id_cls.return_value.identify.assert_not_called()

    def test_uses_enhanced_model_and_spends_a_credit(self, make_client):
        client, db = make_client(_user_with_credits(3), remaining_after_spend=2)
        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=[
                    BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
                ]
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK]
            )
            mock_dedup_cls.return_value.check = AsyncMock(return_value=[ENRICHED_BOOK])
            resp = client.post(
                "/scan", files={"file": _image_file()}, data={"enhanced": "true"}
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["enhanced"] is True
        assert body["enhanced_scan_credits"] == 2
        mock_id_cls.assert_called_once_with(enhanced=True)
        db.commit.assert_awaited_once()

    def test_enhanced_scan_finding_nothing_is_free(self, make_client):
        client, db = make_client(_user_with_credits(3))
        with patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls:
            mock_id_cls.return_value.identify = AsyncMock(return_value=[])
            resp = client.post(
                "/scan", files={"file": _image_file()}, data={"enhanced": "true"}
            )

        assert resp.status_code == 200
        assert resp.json()["enhanced_scan_credits"] == 3
        db.execute.assert_not_called()
        db.commit.assert_not_called()

    def test_standard_scan_does_not_spend_credits(self, make_client):
        client, db = make_client(_user_with_credits(3))
        with (
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService") as mock_enrich_cls,
            patch("app.api.scan.DeduplicationService") as mock_dedup_cls,
        ):
            mock_id_cls.return_value.identify = AsyncMock(
                return_value=[
                    BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
                ]
            )
            mock_enrich_cls.return_value.enrich = AsyncMock(
                return_value=[ENRICHED_BOOK]
            )
            mock_dedup_cls.return_value.check = AsyncMock(return_value=[ENRICHED_BOOK])
            resp = client.post("/scan", files={"file": _image_file()})

        assert resp.json()["enhanced_scan_credits"] == 3
        db.commit.assert_not_called()


class TestTurnstileProtection:
    """When TURNSTILE_SECRET_KEY is configured, /scan requires a valid Turnstile token."""

    @pytest.fixture
    def client_with_turnstile(self):
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db

        with patch("app.api.scan.settings") as mock_settings:
            mock_settings.rate_limit_scan = "10/minute"
            mock_settings.turnstile_required = True
            mock_settings.turnstile_secret_key = (
                "0x0000000000000000000000000000000000000000"
            )
            mock_settings.clamav_enabled = False
            yield TestClient(app), mock_settings

        app.dependency_overrides.clear()

    def test_returns_400_when_token_missing(self, client_with_turnstile):
        client, _ = client_with_turnstile
        resp = client.post("/scan", files={"file": _image_file()})
        assert resp.status_code == 400
        assert "Turnstile" in resp.json()["detail"]

    def test_returns_403_when_token_invalid(self, client_with_turnstile):
        client, _ = client_with_turnstile
        with patch("app.api.scan._verify_turnstile", new=AsyncMock(return_value=False)):
            resp = client.post(
                "/scan",
                files={"file": _image_file()},
                data={"cf-turnstile-response": "bad-token"},
            )
        assert resp.status_code == 403
        assert "Turnstile" in resp.json()["detail"]

    def test_proceeds_when_token_valid(self, client_with_turnstile):
        client, _ = client_with_turnstile
        with (
            patch("app.api.scan._verify_turnstile", new=AsyncMock(return_value=True)),
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_id_cls.return_value.identify = AsyncMock(return_value=[])
            resp = client.post(
                "/scan",
                files={"file": _image_file()},
                data={"cf-turnstile-response": "valid-token"},
            )
        assert resp.status_code == 200

    def test_skipped_when_turnstile_disabled(self):
        """When TURNSTILE_REQUIRED=false, the check is skipped entirely."""
        from app.auth.dependencies import get_current_user
        from app.core.database import get_db

        async def _fake_db():
            yield AsyncMock()

        app.dependency_overrides[get_current_user] = lambda: FAKE_USER
        app.dependency_overrides[get_db] = _fake_db

        with (
            patch("app.api.scan.settings") as mock_settings,
            patch("app.api.scan.ChatGPTVisionIdentifier") as mock_id_cls,
            patch("app.api.scan.EnrichmentService"),
            patch("app.api.scan.DeduplicationService"),
        ):
            mock_settings.rate_limit_scan = "10/minute"
            mock_settings.turnstile_required = False  # explicitly disabled
            mock_settings.clamav_enabled = False
            mock_id_cls.return_value.identify = AsyncMock(return_value=[])
            client = TestClient(app)
            resp = client.post("/scan", files={"file": _image_file()})

        app.dependency_overrides.clear()
        assert resp.status_code == 200
