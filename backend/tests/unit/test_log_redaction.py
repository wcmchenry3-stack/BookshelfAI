"""Log redaction filter and PII-free log lines (auth + vision)."""

import hashlib
import logging
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.logging import JsonFormatter, RedactingFilter, redact
from app.main import app

# Built at runtime: three long base64url-ish segments, the shape the redactor
# matches. No token-shaped literal is committed (keeps secret scanners quiet).
JWT = ".".join(seg * 3 for seg in ("headerSeg", "payloadSeg", "signatureS"))


def _record(msg, args=()):
    return logging.LogRecord("t", logging.INFO, __file__, 1, msg, args, None)


class TestRedactingFilter:
    def test_redacts_email_in_message(self):
        r = _record("hello alice@example.com bye")
        assert RedactingFilter().filter(r) is True
        assert "alice@example.com" not in r.getMessage()

    def test_redacts_jwt_and_bearer_in_message(self):
        r = _record(f"tok {JWT} and Bearer abc.def-ghi_123")
        RedactingFilter().filter(r)
        out = r.getMessage()
        assert JWT not in out
        assert "abc.def-ghi_123" not in out

    def test_redacts_string_args_and_keeps_non_strings(self):
        r = _record("%s %s %d", ("bob@example.org", JWT, 7))
        RedactingFilter().filter(r)
        assert r.getMessage().endswith(" 7")
        assert "bob@example.org" not in r.getMessage()
        assert JWT not in r.getMessage()

    def test_non_string_args_rendered_unchanged(self):
        r = _record("%s %s %d", (None, 1.5, 7))
        RedactingFilter().filter(r)
        assert r.getMessage() == "None 1.5 7"

    def test_redacts_strings_nested_in_args(self):
        class Holder:
            def __str__(self):
                return "holder dan@example.com"

        r = _record("%s %s %s", (["carol@example.com"], ("x", JWT), Holder()))
        RedactingFilter().filter(r)
        out = r.getMessage()
        assert "carol@example.com" not in out
        assert "dan@example.com" not in out
        assert JWT not in out

    def test_redacts_credentials_in_url_query(self):
        out = redact("GET https://api.example.com/v1/x?q=dune&key=SeCrEt123&n=1")
        assert "SeCrEt123" not in out
        assert "q=dune" in out and "n=1" in out

    def test_redacts_exception_text(self):
        try:
            raise ValueError(f"bad user erin@example.com token {JWT}")
        except ValueError:
            r = logging.LogRecord(
                "t", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        RedactingFilter().filter(r)
        out = JsonFormatter().format(r)
        assert "erin@example.com" not in out
        assert JWT not in out
        assert "ValueError" in out

    def test_formatter_redacts_without_filter(self):
        try:
            raise RuntimeError("frank@example.com")
        except RuntimeError:
            r = logging.LogRecord(
                "t",
                logging.ERROR,
                __file__,
                1,
                "x %s",
                ("g@example.com",),
                sys.exc_info(),
            )
        out = JsonFormatter().format(r)
        assert "frank@example.com" not in out
        assert "g@example.com" not in out

    def test_dict_args(self):
        r = _record("%(e)s %(n)d", {"e": "c@example.com", "n": 3})
        RedactingFilter().filter(r)
        assert r.getMessage() == "[REDACTED_EMAIL] 3"

    def test_dotted_names_not_redacted(self):
        assert redact("app.services.chatgpt_vision v1.2.3") == (
            "app.services.chatgpt_vision v1.2.3"
        )

    def test_never_raises(self):
        r = _record("x")
        r.args = 5  # unexpected shape
        assert RedactingFilter().filter(r) is True
        with patch("app.core.logging.redact", side_effect=RuntimeError):
            assert RedactingFilter().filter(_record("a@b.com")) is True

    def test_formatter_output_after_filter(self):
        r = _record("user %s", ("z@example.com",))
        RedactingFilter().filter(r)
        assert "z@example.com" not in JsonFormatter().format(r)

    def test_configure_logging_installs_filter(self):
        from app.core.logging import configure_logging

        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        uv_handler = logging.StreamHandler()
        uv_logger = logging.getLogger("uvicorn.access")
        uv_logger.addHandler(uv_handler)
        try:
            configure_logging()
            assert any(
                isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters
            )
            assert any(isinstance(f, RedactingFilter) for f in uv_handler.filters)
        finally:
            uv_logger.removeHandler(uv_handler)
            root.handlers = saved_handlers
            root.setLevel(saved_level)


class TestVisionParseFailureLogs:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content", ["not json SECRET-BOOK-TEXT", '"SECRET-BOOK-TEXT"']
    )
    async def test_no_content_logged(self, caplog, content):
        from app.ai.providers import openai_vision
        from app.ai.registry import FEATURES

        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": content}, "finish_reason": "length"}]
        }
        client = MagicMock()
        client.post = AsyncMock(return_value=resp)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with (
            patch.object(openai_vision.httpx, "AsyncClient", return_value=client),
            caplog.at_level(logging.INFO),
        ):
            result = await openai_vision.OpenAIVisionProvider(
                FEATURES["scan.standard"]
            ).identify(b"img")
        assert result.candidates == []
        assert "SECRET-BOOK-TEXT" not in caplog.text
        assert hashlib.sha256(content.encode()).hexdigest()[:12] in caplog.text
        assert f"length={len(content)}" in caplog.text
        assert "finish_reason=length" in caplog.text


class TestAuthFlowLogs:
    def _post(self, claims, allowed, existing):
        from app.core.database import get_db
        from tests.unit.test_auth_endpoint import _db_returning

        app.dependency_overrides[get_db] = _db_returning(existing)
        try:
            with (
                patch("app.api.auth.verify_google_id_token", return_value=claims),
                patch("app.api.auth.settings") as st,
                patch("app.api.auth.create_access_token", return_value="a"),
                patch("app.api.auth.create_refresh_token", return_value=("r", "j")),
            ):
                st.allowed_emails_list = allowed
                st.jwt_expiry_hours = 24
                st.refresh_token_expiry_days = 7
                return TestClient(app).post("/auth/google", json={"id_token": "t"})
        finally:
            app.dependency_overrides.clear()

    def test_success_paths_log_no_email(self, caplog):
        from tests.unit.test_auth_endpoint import GOOGLE_CLAIMS, _mock_user

        with caplog.at_level(logging.INFO):
            assert self._post(GOOGLE_CLAIMS, [], None).status_code == 200
            assert self._post(GOOGLE_CLAIMS, [], _mock_user()).status_code == 200
        assert "test@example.com" not in caplog.text

    def test_unverified_and_allowlist_log_no_email_or_allowlist(self, caplog):
        from tests.unit.test_auth_endpoint import GOOGLE_CLAIMS

        with caplog.at_level(logging.INFO):
            r1 = self._post({**GOOGLE_CLAIMS, "email_verified": False}, [], None)
            r2 = self._post(GOOGLE_CLAIMS, ["secret-allowed@example.com"], None)
        assert (r1.status_code, r2.status_code) == (401, 403)
        assert "test@example.com" not in caplog.text
        assert "secret-allowed@example.com" not in caplog.text


class TestUvicornAccessLogs:
    """uvicorn's AccessFormatter unpacks record.args as a 5-tuple."""

    def _access_record(self):
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5678", "GET", "/books?token=abc123&e=h@example.com", "1.1", 200),
            None,
        )

    def test_preserve_args_mode_keeps_access_formatter_working(self):
        from uvicorn.logging import AccessFormatter

        record = self._access_record()
        RedactingFilter(preserve_args=True).filter(record)
        out = AccessFormatter(
            fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
        ).format(record)
        assert "GET /books" in out and "200" in out
        assert "abc123" not in out
        assert "h@example.com" not in out

    def test_configure_logging_uses_preserve_args_for_uvicorn(self):
        from app.core.logging import configure_logging

        root = logging.getLogger()
        saved_handlers, saved_level = list(root.handlers), root.level
        uv_handler = logging.StreamHandler()
        uv_logger = logging.getLogger("uvicorn.access")
        uv_logger.addHandler(uv_handler)
        try:
            configure_logging()
            filters = [f for f in uv_handler.filters if isinstance(f, RedactingFilter)]
            assert filters and all(f.preserve_args for f in filters)
        finally:
            uv_logger.removeHandler(uv_handler)
            root.handlers = saved_handlers
            root.setLevel(saved_level)


def test_preserve_args_keeps_placeholder_count():
    """A format string with placeholders is left alone so formatting can't break."""
    r = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        "GET /x?token=%s for %s@example.com",
        ("abc123", "z"),
        None,
    )
    RedactingFilter(preserve_args=True).filter(r)
    assert r.getMessage() == "GET /x?token=abc123 for z@example.com"
