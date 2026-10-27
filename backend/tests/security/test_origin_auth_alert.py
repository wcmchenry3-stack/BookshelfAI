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
