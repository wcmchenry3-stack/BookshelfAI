"""Sentry never receives a request body or cookies (#1053).

User-entered text (private notes, titles, search text) travels in JSON bodies.
Uses a real client with the app's privacy options and a real request.
"""

import json

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from app.core.sentry_context import _scrub_event, sentry_privacy_options

pytestmark = pytest.mark.security

NOTES = "Private: lent to my sister, body leak probe"


def _event() -> dict:
    return {
        "request": {
            "method": "PATCH",
            "url": "http://testserver/user-books/1",
            "data": {"notes": NOTES},
            "cookies": {"session": "abc"},
            "headers": {"content-type": "application/json"},
        }
    }


class TestScrubber:
    def test_error_event_has_no_body(self):
        out = sentry_privacy_options()["before_send"](_event())
        assert "data" not in out["request"]
        assert "cookies" not in out["request"]
        assert NOTES not in json.dumps(out)
        assert out["request"]["method"] == "PATCH"
        assert out["request"]["url"] == "http://testserver/user-books/1"
        assert out["request"]["headers"] == {"content-type": "application/json"}

    def test_transaction_event_has_no_body(self):
        event = {**_event(), "type": "transaction", "spans": []}
        out = sentry_privacy_options()["before_send_transaction"](event)
        assert "data" not in out["request"]
        assert NOTES not in json.dumps(out)

    def test_both_hooks_use_the_scrubber(self):
        options = sentry_privacy_options()
        assert options["before_send"] is _scrub_event
        assert options["before_send_transaction"] is _scrub_event

    def test_init_never_captures_bodies(self):
        assert sentry_privacy_options()["max_request_body_size"] == "never"


@pytest.fixture
def payloads():
    got: list[dict] = []

    class _Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") in ("event", "transaction"):
                    got.append(item.payload.json)

    with sentry_sdk.isolation_scope(), sentry_sdk.new_scope():
        client = sentry_sdk.Client(
            dsn="https://abc@o1.ingest.us.sentry.io/1",
            transport=_Capture,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            traces_sample_rate=1.0,
            send_default_pii=False,
            **sentry_privacy_options(),
        )
        sentry_sdk.get_global_scope().set_client(client)
        try:
            yield got
        finally:
            client.close()
            sentry_sdk.get_global_scope().set_client(None)


def test_real_request_transaction_and_error_carry_no_body(payloads):
    app = FastAPI()

    @app.patch("/notes")
    async def patch_notes(body: dict):
        return {"ok": True}

    @app.patch("/boom")
    async def boom(body: dict):
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    assert client.patch("/notes", json={"notes": NOTES}).status_code == 200
    assert client.patch("/boom", json={"notes": NOTES}).status_code == 500
    sentry_sdk.flush()

    kinds = {p.get("type", "error") for p in payloads}
    assert "transaction" in kinds and "error" in kinds
    assert NOTES not in json.dumps(payloads, default=str)
    for p in payloads:
        assert "data" not in (p.get("request") or {})
