"""Security tests — Sentry alert for bearer-carrying origin-lockdown rejections (#721)."""

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import origin_auth
from app.core.config import settings
from app.core.origin_auth import OriginAuthMiddleware

pytestmark = pytest.mark.security

SECRET = "unit-test-origin-secret-value-0123456789"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.super-secret-bearer-payload.sig"
ALERT = "origin_auth_rejected_authenticated"


class FakeScope:
    def __init__(self, sink):
        self.sink = sink

    def set_tag(self, k, v):
        self.sink["tags"][k] = v

    def set_extra(self, k, v):
        self.sink["extras"][k] = v

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def alerts(monkeypatch):
    monkeypatch.setattr(settings, "origin_auth_secret", SECRET)
    events: list[dict] = []
    current: dict = {}

    def isolation_scope():
        current.clear()
        current.update(tags={}, extras={})
        return FakeScope(current)

    def capture_message(message, level=None):
        events.append(
            {
                "message": message,
                "level": level,
                "tags": dict(current["tags"]),
                "extras": dict(current["extras"]),
            }
        )

    monkeypatch.setattr(origin_auth.sentry_sdk, "isolation_scope", isolation_scope)
    monkeypatch.setattr(origin_auth.sentry_sdk, "capture_message", capture_message)
    clock = {"t": 1000.0}
    monkeypatch.setattr(origin_auth, "_clock", lambda: clock["t"])
    monkeypatch.setattr(origin_auth, "_last_alert_at", None)
    monkeypatch.setattr(origin_auth, "_rejected_since_alert", 0)
    return events, clock


@pytest.fixture
def client():
    app = FastAPI()

    @app.get("/x")
    def x():
        return {"ok": True}

    return TestClient(OriginAuthMiddleware(app))


def _bearer(scheme="Bearer"):
    return {"Authorization": f"{scheme} {TOKEN}"}


def test_authenticated_rejection_captured_once_per_window(alerts, client):
    events, clock = alerts
    assert client.get("/x", headers=_bearer()).status_code == 403
    assert len(events) == 1
    assert events[0]["message"] == ALERT
    assert events[0]["level"] == "error"
    assert events[0]["tags"] == {"method": "GET", "path": "/x"}
    assert events[0]["extras"]["rejected_since_last"] == 1

    clock["t"] += 299
    client.get("/x", headers=_bearer())
    client.get("/x", headers=_bearer("bearer"))
    assert len(events) == 1

    clock["t"] += 2
    client.get("/x", headers=_bearer("BEARER"))
    assert len(events) == 2
    assert events[1]["extras"]["rejected_since_last"] == 3


def test_second_authorization_header_bearer_counts(alerts):
    _ = alerts
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [
            (b"authorization", b"Basic abc"),
            (b"authorization", b"Bearer a.b.c"),
        ],
    }
    assert origin_auth._has_bearer(scope)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/api/books/123", "/api/books/:id"),
        ("/api/books/123/notes/7", "/api/books/:id/notes/:id"),
        (
            "/api/x/123e4567-e89b-12d3-a456-426614174000/y",
            "/api/x/:id/y",
        ),
        ("/api/v2/books", "/api/v2/books"),
        ("/" + "a" * 500, ("/" + "a" * 500)[:200]),
    ],
)
def test_path_tag_collapses_ids(alerts, path, expected):
    events, _ = alerts
    app = FastAPI()
    c = TestClient(OriginAuthMiddleware(app))
    c.get(path, headers=_bearer())
    assert events[0]["tags"]["path"] == expected


def test_unauthenticated_rejection_not_captured(alerts, client, caplog):
    events, _ = alerts
    with caplog.at_level(logging.DEBUG):
        assert client.get("/x").status_code == 403
        assert (
            client.get("/x", headers={"Authorization": "Basic abc"}).status_code == 403
        )
    assert events == []
    rejections = [r for r in caplog.records if "origin_auth" in r.getMessage()]
    assert rejections
    assert all(r.levelno == logging.INFO for r in rejections)


def test_secret_header_and_token_absent(alerts, client, caplog):
    events, _ = alerts
    with caplog.at_level(logging.DEBUG):
        client.get("/x", headers={**_bearer(), "X-Origin-Auth": "wrong-value"})
    blob = (repr(events) + caplog.text).lower().replace(ALERT, "")
    for forbidden in (SECRET, TOKEN, "wrong-value", "x-origin-auth", "bearer"):
        assert forbidden.lower() not in blob
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert ALERT in warnings[0].getMessage()


def test_allowed_request_unchanged_and_no_alert(alerts, client):
    events, _ = alerts
    r = client.get("/x", headers={**_bearer(), "X-Origin-Auth": SECRET})
    assert r.status_code == 200
    assert events == []


# ---------------------------------------------------------------------------
# Real Sentry client through the full app.main stack
# ---------------------------------------------------------------------------


def test_real_sentry_event_has_no_secret_token_or_header_value(monkeypatch):
    import json

    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
    from sentry_sdk.transport import Transport

    from app.core.sentry_context import sentry_privacy_options
    from app.main import app

    class MemoryTransport(Transport):
        def __init__(self, options=None):
            super().__init__(options)
            self.events: list[dict] = []

        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    self.events.append(item.payload.json)

    monkeypatch.setattr(settings, "origin_auth_secret", SECRET)
    monkeypatch.setattr(origin_auth, "_last_alert_at", None)
    monkeypatch.setattr(origin_auth, "_rejected_since_alert", 0)
    transport = MemoryTransport()
    scope = sentry_sdk.get_global_scope()
    previous_client = scope.client
    try:
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
                headers = {
                    **_bearer(),
                    settings.origin_auth_header: "wrong-origin-value",
                }
                with TestClient(app) as client:
                    assert (
                        client.get("/api/books/42", headers=headers).status_code == 403
                    )
                    assert (
                        client.get("/api/books/43", headers=headers).status_code == 403
                    )
            finally:
                sentry_sdk.get_client().close()
    finally:
        scope.set_client(previous_client)

    assert len(transport.events) == 1
    event = transport.events[0]
    assert event["message"] == ALERT
    assert event["level"] == "error"
    assert event["tags"]["path"] == "/api/books/:id"
    blob = json.dumps(event).lower()
    for forbidden in (SECRET, TOKEN, "wrong-origin-value"):
        assert forbidden.lower() not in blob


def test_throttled_bearer_rejection_logs_info_not_warning(alerts, client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.get("/x", headers=_bearer())
        client.get("/x", headers=_bearer())
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "count=1" in warnings[0].getMessage()
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and r.name == origin_auth.__name__
    ]
    assert len(infos) == 1
    assert "bearer=1" in infos[0].getMessage()


@pytest.mark.parametrize("value", ["Bearer x", "Bearer a.b", "Bearer a..c", "Bearer "])
def test_non_jwt_shaped_bearer_does_not_alert(alerts, client, value):
    events, _ = alerts
    assert client.get("/x", headers={"Authorization": value}).status_code == 403
    assert events == []


def test_jwt_shaped_bearer_alerts(alerts, client):
    events, _ = alerts
    client.get("/x", headers={"Authorization": "Bearer aa.bb-_.cc"})
    assert len(events) == 1


def test_control_chars_sanitized_in_tag_and_log(alerts, client, caplog):
    events, _ = alerts
    with caplog.at_level(logging.DEBUG):
        client.get("/x%0Afake%0Dline", headers=_bearer())
    assert events[0]["tags"]["path"] == "/x?fake?line"
    for r in caplog.records:
        assert "\n" not in r.getMessage()
        assert "\r" not in r.getMessage()


def test_method_capped():
    scope = {"type": "http", "method": "M" * 50, "path": "/x", "headers": []}
    assert origin_auth._clean(scope["method"], origin_auth._MAX_METHOD) == "M" * 16
