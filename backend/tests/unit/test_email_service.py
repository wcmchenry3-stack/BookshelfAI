"""Transactional email through Resend (#694), against ``httpx.MockTransport``.

The request (sender on the verified domain, reply-to, both bodies, the API
key, Resend's idempotency key), the bounded retries, errors that carry only
the status, and ``configured()``. Nothing here calls Resend.
"""

import json
import uuid
from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.services import _http_retry, email
from app.services.email import EmailError, Message

API_KEY = f"resend-{uuid.uuid5(uuid.NAMESPACE_URL, 'email-test')}"
TO = "reader@example.com"
MESSAGE = Message(to=TO, subject="Subject", html="<p>Hi</p>", text="Hi")


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(_http_retry, "BACKOFF_S", 0)
    with (
        patch.object(settings, "resend_api_key", API_KEY),
        patch.object(settings, "email_from", "no-reply@mail.example.com"),
        patch.object(settings, "email_reply_to", "help@example.com"),
    ):
        yield


def _transport(*replies):
    requests: list[httpx.Request] = []
    queue = list(replies)

    def handler(request):
        requests.append(request)
        reply = queue.pop(0) if queue else httpx.Response(200, json={"id": "e"})
        if isinstance(reply, Exception):
            raise reply
        return reply

    return httpx.MockTransport(handler), requests


async def test_send_posts_the_message_to_resend():
    transport, requests = _transport(httpx.Response(200, json={"id": "e1"}))
    await email.send(MESSAGE, idempotency_key="account-export-1", transport=transport)
    (req,) = requests
    assert str(req.url) == email.API_URL == "https://api.resend.com/emails"
    assert req.headers["authorization"] == f"Bearer {API_KEY}"
    assert req.headers["idempotency-key"] == "account-export-1"
    assert json.loads(req.content) == {
        "from": "Bookshelf <no-reply@mail.example.com>",
        "to": [TO],
        "reply_to": "help@example.com",
        "subject": "Subject",
        "html": "<p>Hi</p>",
        "text": "Hi",
    }


async def test_a_5xx_is_retried_then_succeeds():
    transport, requests = _transport(httpx.Response(500), httpx.Response(200))
    await email.send(MESSAGE, idempotency_key="k", transport=transport)
    assert len(requests) == 2


async def test_a_refusal_is_final_and_carries_the_status_only(caplog):
    transport, requests = _transport(httpx.Response(422, json={"message": TO}))
    with pytest.raises(EmailError) as exc_info, caplog.at_level("WARNING"):
        await email.send(MESSAGE, idempotency_key="k", transport=transport)
    assert len(requests) == 1
    assert exc_info.value.status == 422
    for text in (str(exc_info.value), caplog.text):
        assert TO not in text and API_KEY not in text


async def test_unreachable_after_the_retries():
    transport, requests = _transport(*(httpx.ConnectError("down") for _ in range(3)))
    with pytest.raises(EmailError) as exc_info:
        await email.send(MESSAGE, idempotency_key="k", transport=transport)
    assert len(requests) == 3
    assert exc_info.value.status is None


def test_configured_follows_the_api_key():
    assert email.configured()
    with patch.object(settings, "resend_api_key", ""):
        assert not email.configured()
