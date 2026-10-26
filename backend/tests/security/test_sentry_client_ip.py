"""Sentry events must not carry the end user's IP address or location (#565).

Uses a REAL Sentry client (default integrations on, FastAPI/Starlette
integrations as in ``app/main.py``, the app's real privacy options) with an
in-memory transport and a real request, then checks the serialised event.
"""

import json

import pytest
import sentry_sdk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.transport import Transport

from app.core.sentry_context import SentryContextMiddleware, sentry_privacy_options

pytestmark = pytest.mark.security

CLIENT_IP = "203.0.113.77"
CLIENT_IPV6 = "2001:db8:abcd:12::77"

# Mixed case on purpose: header names are case-insensitive.
IP_HEADERS = {
    "CF-Connecting-IP": CLIENT_IP,
    "True-Client-IP": CLIENT_IP,
    "Cf-Connecting-IPv6": CLIENT_IPV6,
    "CF-Pseudo-IPv4": CLIENT_IP,
    "X-Client-IP": CLIENT_IP,
    "Forwarded": f"for={CLIENT_IP};proto=https",
    "X-Forwarded-For": CLIENT_IP,
    "X-Real-IP": CLIENT_IP,
    "CF-IPCountry": "NL",
}


class _MemoryTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                self.events.append(item.payload.json)


@pytest.fixture
def sentry_events():
    """A real client, isolated: the previous client/scopes are restored."""
    transport = _MemoryTransport()
    scope = sentry_sdk.get_global_scope()
    previous_client = scope.client
    with sentry_sdk.isolation_scope(), sentry_sdk.new_scope():
        sentry_sdk.init(
            dsn="https://public@example.invalid/1",
            transport=transport,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            traces_sample_rate=0.0,
            send_default_pii=False,
            **sentry_privacy_options(),
        )
        try:
            yield transport.events
        finally:
            sentry_sdk.get_client().close()
    scope.set_client(previous_client)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SentryContextMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    @app.get("/capture")
    async def capture():
        sentry_sdk.capture_message("captured")
        return {"ok": True}

    return app


@pytest.mark.parametrize("path", ["/boom", "/capture"])
def test_no_client_ip_or_location_in_event(sentry_events, path):
    client = TestClient(_app(), raise_server_exceptions=False)
    client.get(path, headers={**IP_HEADERS, "Authorization": "Bearer t0k3n"})
    sentry_sdk.flush()

    assert sentry_events, "no event captured"
    for event in sentry_events:
        blob = json.dumps(event)
        for value in (CLIENT_IP, CLIENT_IPV6, 'NL"'):
            assert value not in blob, f"{value} leaked: {blob}"
        names = {k.lower() for k in (event.get("request", {}).get("headers") or {})}
        for header in IP_HEADERS:
            assert header.lower() not in names
        assert "ip_address" not in (event.get("user") or {})
        assert "REMOTE_ADDR" not in (event.get("request", {}).get("env") or {})


def test_scrub_removes_env_and_user_ip_if_integrations_set_them():
    from app.core.sentry_context import _scrub_event

    event = {
        "request": {
            "headers": {"CF-Connecting-IP": CLIENT_IP, "cf-ray": "abc"},
            "env": {"REMOTE_ADDR": CLIENT_IP, "SERVER_NAME": "x"},
        },
        "user": {"id": "1", "ip_address": CLIENT_IP},
    }
    out = _scrub_event(event)
    assert out["request"]["headers"] == {"cf-ray": "abc"}
    assert out["request"]["env"] == {"SERVER_NAME": "x"}
    assert out["user"] == {"id": "1"}
