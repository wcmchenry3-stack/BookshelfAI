"""Sentry release tagging and privacy — behavioural tests.

The pipeline tests run a real ``sentry_sdk`` client with a capturing transport
(no network) and assert on the serialized payload, because the shapes the SDK
actually produces (repr'd frame locals, span data, breadcrumbs) are exactly
where a key-name scrubber gives false assurance.
"""

import json

import pytest
import sentry_sdk
from pydantic import BaseModel
from sentry_sdk.scrubber import DEFAULT_DENYLIST
from sentry_sdk.transport import Transport

from app.core.config import Settings
from app.core.sentry_context import (
    _settings_secret_names,
    build_event_scrubber,
    sentry_privacy_options,
)

_DB_URL = "postgresql+asyncpg://u:p@localhost/db"
_FILTERED = "[Filtered]"


def _settings(**overrides) -> Settings:
    return Settings(database_url=_DB_URL, turnstile_required=False, **overrides)


def _is_filtered(value) -> bool:
    """The scrubber swaps a sensitive value for an annotated "[Filtered]"."""
    return getattr(value, "value", value) == _FILTERED


class TestSentryRelease:
    def test_release_built_from_render_git_commit(self):
        s = _settings(render_git_commit="0123456789abcdef0123456789abcdef01234567")
        assert (
            s.sentry_release == "bookshelf-api@0123456789abcdef0123456789abcdef01234567"
        )

    @pytest.mark.parametrize("sha", ["", "   "])
    def test_release_is_none_without_a_render_commit(self, sha):
        """None hands release detection back to the SDK (it does NOT mean "no release")."""
        assert _settings(render_git_commit=sha).sentry_release is None

    def test_render_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
        assert _settings().sentry_release == "bookshelf-api@abc123"


class TestDenylist:
    def test_keeps_sentry_defaults(self):
        denylist = {k.lower() for k in build_event_scrubber().denylist}
        assert {k.lower() for k in DEFAULT_DENYLIST} <= denylist

    def test_every_secret_looking_settings_field_is_covered(self):
        """Derived from Settings.model_fields, so a new secret cannot be forgotten."""
        names = set(_settings_secret_names())
        assert {
            "database_url",
            "jwt_private_key",
            "google_client_secret",
            "turnstile_secret_key",
            "openai_api_key",
            "google_books_api_key",
            "test_auth_secret",
            "sentry_dsn",
        } <= names
        assert names <= {k.lower() for k in build_event_scrubber().denylist}

    @pytest.mark.parametrize("key", ["id_token", "refresh_token", "access_token"])
    def test_auth_tokens_in_request_body_are_filtered(self, key):
        """The SDK default only matches the exact key "token" — these slip past it."""
        event = {"request": {"data": {key: "eyJhbGciOi.real.token", "ok": "keep"}}}
        build_event_scrubber().scrub_event(event)
        assert _is_filtered(event["request"]["data"][key])
        assert event["request"]["data"]["ok"] == "keep"

    def test_nested_credentials_are_filtered(self):
        # "body" is not a denylisted key, so only recursion can reach the token.
        event = {
            "extra": {"payload": {"body": {"refresh_token": "v"}, "book_id": "42"}}
        }
        build_event_scrubber().scrub_event(event)
        assert _is_filtered(event["extra"]["payload"]["body"]["refresh_token"])
        assert event["extra"]["payload"]["book_id"] == "42"

    def test_turnstile_and_authorization_headers_are_filtered(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer abc",
                    "cf-turnstile-response": "0.turnstile-token",
                    "User-Agent": "BookShelfAI/1.0",
                }
            }
        }
        build_event_scrubber().scrub_event(event)
        headers = event["request"]["headers"]
        assert _is_filtered(headers["Authorization"])
        assert _is_filtered(headers["cf-turnstile-response"])
        assert headers["User-Agent"] == "BookShelfAI/1.0"


class _RefreshRequest(BaseModel):
    refresh_token: str


_SECRETS = {
    "pydantic-model local": "REFRESH-SECRET-123",
    "cf_turnstile_response local": "TURNSTILE-SECRET-456",
    "new_refresh local": "ROTATED-SECRET-789",
    "params['key'] local": "AIzaFAKEGOOGLEBOOKSKEY",
    "http.query span/breadcrumb": "AIzaQUERYSTRINGKEY",
}


def _handler(body: _RefreshRequest, cf_turnstile_response: str, new_refresh: str):
    """Mirrors the local names in app/api/auth.py, app/api/scan.py, google_books.py."""
    params = {"q": "dune", "key": _SECRETS["params['key'] local"]}  # noqa: F841
    raise RuntimeError("boom")


@pytest.fixture
def captured():
    """A real SDK client wired with the app's privacy options; payloads land here."""
    payloads: list[dict] = []

    class _Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") in ("event", "transaction"):
                    payloads.append(item.payload.json)

    with sentry_sdk.isolation_scope(), sentry_sdk.new_scope():
        client = sentry_sdk.Client(
            dsn="https://abc@o1.ingest.us.sentry.io/1",
            transport=_Capture,
            send_default_pii=False,
            traces_sample_rate=1.0,
            default_integrations=False,
            **sentry_privacy_options(),
        )
        sentry_sdk.get_global_scope().set_client(client)
        try:
            yield payloads
        finally:
            client.close()
            sentry_sdk.get_global_scope().set_client(None)


class TestRealPipeline:
    def test_no_secret_in_a_captured_exception(self, captured):
        key = _SECRETS["http.query span/breadcrumb"]
        sentry_sdk.add_breadcrumb(
            type="http",
            category="httplib",
            data={
                "url": f"https://www.googleapis.com/books/v1/volumes?q=dune&key={key}",
                "http.query": f"q=dune&key={key}",
            },
        )
        try:
            _handler(
                _RefreshRequest(refresh_token=_SECRETS["pydantic-model local"]),
                _SECRETS["cf_turnstile_response local"],
                _SECRETS["new_refresh local"],
            )
        except RuntimeError:
            sentry_sdk.capture_exception()
        sentry_sdk.flush()

        assert len(captured) == 1
        blob = json.dumps(captured[0])
        leaked = [label for label, secret in _SECRETS.items() if secret in blob]
        assert leaked == []
        # The event is still useful: type, message and stack are intact.
        exc = captured[0]["exception"]["values"][0]
        assert exc["type"] == "RuntimeError"
        assert exc["stacktrace"]["frames"][-1]["function"] == "_handler"
        assert "vars" not in exc["stacktrace"]["frames"][-1]

    def test_no_query_string_in_a_captured_transaction(self, captured):
        key = _SECRETS["http.query span/breadcrumb"]
        url = f"https://www.googleapis.com/books/v1/volumes?q=dune&key={key}"
        with (
            sentry_sdk.start_transaction(name="GET /books/search", op="http.server"),
            sentry_sdk.start_span(op="http.client", name=f"GET {url}") as span,
        ):
            span.set_data("url", url)
            span.set_data("http.query", f"q=dune&key={key}")
        sentry_sdk.flush()

        assert len(captured) == 1
        assert key not in json.dumps(captured[0])
        span_data = captured[0]["spans"][0]["data"]
        assert span_data["url"] == "https://www.googleapis.com/books/v1/volumes"


class TestPrivacyOptions:
    def test_frame_locals_are_never_sent(self):
        assert sentry_privacy_options()["include_local_variables"] is False

    def test_all_three_hooks_are_installed(self):
        options = sentry_privacy_options()
        for hook in ("before_send", "before_send_transaction", "before_breadcrumb"):
            assert callable(options[hook])

    def test_main_uses_the_privacy_options(self):
        """main.py must splat the options into init rather than re-listing them."""
        from app import main

        assert main.sentry_privacy_options is sentry_privacy_options
