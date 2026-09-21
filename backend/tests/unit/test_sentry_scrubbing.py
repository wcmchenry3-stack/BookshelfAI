"""Sentry release tagging and PII scrubbing — behavioural tests."""

import copy

import pytest
from sentry_sdk._types import AnnotatedValue
from sentry_sdk.scrubber import DEFAULT_DENYLIST

from app.core.config import Settings
from app.core.sentry_context import build_event_scrubber

_DB_URL = "postgresql+asyncpg://u:p@localhost/db"


def _is_filtered(value) -> bool:
    """The scrubber swaps a sensitive value for an AnnotatedValue("[Filtered]")."""
    return isinstance(value, AnnotatedValue) and value.value == "[Filtered]"


def _settings(**overrides) -> Settings:
    return Settings(database_url=_DB_URL, turnstile_required=False, **overrides)


class TestSentryRelease:
    def test_release_built_from_render_git_commit(self):
        s = _settings(render_git_commit="0123456789abcdef0123456789abcdef01234567")
        assert (
            s.sentry_release == "bookshelf-api@0123456789abcdef0123456789abcdef01234567"
        )

    @pytest.mark.parametrize("sha", ["", "   "])
    def test_no_release_when_commit_unknown(self, sha):
        """Locally and in CI there is no commit — send no release rather than a fake one."""
        assert _settings(render_git_commit=sha).sentry_release is None

    def test_render_env_var_is_read(self, monkeypatch):
        monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
        assert _settings().sentry_release == "bookshelf-api@abc123"


class TestEventScrubber:
    def _scrub(self, event: dict) -> dict:
        event = copy.deepcopy(event)
        build_event_scrubber().scrub_event(event)
        return event

    def test_keeps_sentry_defaults(self):
        denylist = build_event_scrubber().denylist
        assert {k.lower() for k in DEFAULT_DENYLIST} <= {k.lower() for k in denylist}

    @pytest.mark.parametrize("key", ["id_token", "refresh_token", "access_token"])
    def test_auth_tokens_in_request_body_are_filtered(self, key):
        """The SDK default only matches the exact key "token" — these slip past it."""
        event = self._scrub(
            {"request": {"data": {key: "eyJhbGciOi.real.token", "ok": "keep"}}}
        )
        assert _is_filtered(event["request"]["data"][key])
        assert event["request"]["data"]["ok"] == "keep"

    def test_nested_credentials_are_filtered(self):
        """recursive=True: credentials nested inside extra/context dicts are scrubbed too."""
        event = self._scrub(
            {
                "extra": {
                    "payload": {
                        # "body" is not a denylisted key, so only recursion
                        # can reach the token two levels down.
                        "body": {"refresh_token": "secret-value"},
                        "book_id": "42",
                    }
                }
            }
        )
        assert _is_filtered(event["extra"]["payload"]["body"]["refresh_token"])
        assert event["extra"]["payload"]["book_id"] == "42"

    def test_turnstile_and_authorization_headers_are_filtered(self):
        event = self._scrub(
            {
                "request": {
                    "headers": {
                        "Authorization": "Bearer abc",
                        "cf-turnstile-response": "0.turnstile-token",
                        "User-Agent": "BookShelfAI/1.0",
                    }
                }
            }
        )
        headers = event["request"]["headers"]
        assert _is_filtered(headers["Authorization"])
        assert _is_filtered(headers["cf-turnstile-response"])
        assert headers["User-Agent"] == "BookShelfAI/1.0"

    def test_frame_locals_holding_config_secrets_are_filtered(self):
        event = self._scrub(
            {
                "exception": {
                    "values": [
                        {
                            "stacktrace": {
                                "frames": [
                                    {
                                        "vars": {
                                            "database_url": "postgresql://u:pw@host/db",
                                            "openai_api_key": "sk-live",
                                            "title": "Dune",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        )
        frame_vars = event["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
        assert _is_filtered(frame_vars["database_url"])
        assert _is_filtered(frame_vars["openai_api_key"])
        assert frame_vars["title"] == "Dune"


class TestInitWiring:
    def test_main_passes_release_and_scrubber_to_sentry_init(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        assert "release=settings.sentry_release" in source
        assert "event_scrubber=build_event_scrubber()" in source
        assert "send_default_pii=False" in source
