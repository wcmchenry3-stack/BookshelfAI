"""The Supabase Storage client (#694), against ``httpx.MockTransport``.

Nothing here reaches the real service: every request goes to the mock,
which records it. Covers the request shapes (upload with upsert, sign,
bulk delete, list), the service-role key going only to the project, the
bounded retries (and which statuses are final), errors that carry no URL,
key or token, and the kill switch (``from_settings`` is ``None`` unset).
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from app.core.config import settings
from app.services import _http_retry, object_storage
from app.services.object_storage import ObjectStorage, StorageError

BASE = "https://project-ref.supabase.co"
# Derived, so no literal secret-looking value sits in the repo.
SERVICE_KEY = f"service-{uuid.uuid5(uuid.NAMESPACE_URL, 'storage-test')}"
SIGN_TOKEN = f"signed-{uuid.uuid5(uuid.NAMESPACE_URL, 'sign-test')}"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(_http_retry, "BACKOFF_S", 0)


class Recorder:
    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.responses.pop(0) if self.responses else httpx.Response(200)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _storage(recorder) -> ObjectStorage:
    return ObjectStorage(
        BASE, SERVICE_KEY, "account-exports", transport=httpx.MockTransport(recorder)
    )


async def test_upload_posts_the_bytes_with_upsert_and_the_key():
    rec = Recorder(httpx.Response(200, json={"Key": "x"}))
    await _storage(rec).upload("exports/a b.json", b"{}", "application/json")
    (req,) = rec.requests
    assert req.method == "POST"
    assert (
        str(req.url) == f"{BASE}/storage/v1/object/account-exports/exports/a%20b.json"
    )
    assert req.headers["authorization"] == f"Bearer {SERVICE_KEY}"
    assert req.headers["apikey"] == SERVICE_KEY
    assert req.headers["x-upsert"] == "true"
    assert req.headers["content-type"] == "application/json"
    assert req.content == b"{}"


async def test_signed_url_asks_for_the_expiry_and_builds_the_full_url():
    signed = f"/object/sign/account-exports/exports/x.json?token={SIGN_TOKEN}"
    rec = Recorder(httpx.Response(200, json={"signedURL": signed}))
    url = await _storage(rec).signed_url(
        "exports/x.json", 604_800, download_name="bookshelf export.json"
    )
    (req,) = rec.requests
    assert (
        str(req.url) == f"{BASE}/storage/v1/object/sign/account-exports/exports/x.json"
    )
    assert json.loads(req.content) == {"expiresIn": 604_800}
    assert url == (f"{BASE}/storage/v1{signed}&download=bookshelf%20export.json")


async def test_signed_url_without_a_query_or_name():
    rec = Recorder(httpx.Response(200, json={"signedURL": "/object/sign/b/x"}))
    assert (
        await _storage(rec).signed_url("x", 60) == f"{BASE}/storage/v1/object/sign/b/x"
    )
    rec = Recorder(httpx.Response(200, json={"signedURL": "/object/sign/b/x"}))
    url = await _storage(rec).signed_url("x", 60, download_name="f.json")
    assert url.endswith("/object/sign/b/x?download=f.json")


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, json={}),
        httpx.Response(200, json={"signedURL": "https://elsewhere.example/x"}),
        httpx.Response(200, json={"signedURL": 3}),
        httpx.Response(200, content=b"not json"),
    ],
)
async def test_a_malformed_sign_reply_is_an_error(reply):
    with pytest.raises(StorageError) as exc_info:
        await _storage(Recorder(reply)).signed_url("x", 60)
    assert exc_info.value.operation == "sign"


async def test_delete_sends_every_path_in_one_call_and_skips_an_empty_list():
    rec = Recorder(httpx.Response(200, json=[]))
    storage = _storage(rec)
    await storage.delete([])
    assert rec.requests == []
    await storage.delete(["exports/a.json", "/exports/b.json"])
    (req,) = rec.requests
    assert req.method == "DELETE"
    assert str(req.url) == f"{BASE}/storage/v1/object/account-exports"
    assert json.loads(req.content) == {"prefixes": ["exports/a.json", "exports/b.json"]}


async def test_list_returns_objects_oldest_first_and_skips_folders():
    rec = Recorder(
        httpx.Response(
            200,
            json=[
                {"name": "a.json", "id": "1", "created_at": "2026-11-01T00:00:00Z"},
                {"name": "sub", "id": None},
                {"name": "b.json", "id": "2", "created_at": "garbage"},
                {"name": "c.json", "id": "3"},
                "not an object",
            ],
        )
    )
    objects = await _storage(rec).list("exports", limit=50)
    (req,) = rec.requests
    assert str(req.url) == f"{BASE}/storage/v1/object/list/account-exports"
    assert json.loads(req.content) == {
        "prefix": "exports",
        "limit": 50,
        "offset": 0,
        "sortBy": {"column": "created_at", "order": "asc"},
    }
    assert [o.name for o in objects] == ["a.json", "b.json", "c.json"]
    assert objects[0].created_at == datetime(2026, 11, 1, tzinfo=UTC)
    assert objects[1].created_at is None and objects[2].created_at is None


@pytest.mark.parametrize(
    "reply", [httpx.Response(200, json={"a": 1}), httpx.Response(200, content=b"<")]
)
async def test_a_malformed_list_reply_is_an_error(reply):
    with pytest.raises(StorageError):
        await _storage(Recorder(reply)).list("exports")


async def test_a_5xx_or_429_or_transport_error_is_retried():
    rec = Recorder(
        httpx.Response(503),
        httpx.ConnectError("refused"),
        httpx.Response(200),
    )
    await _storage(rec).upload("x", b"1", "application/json")
    assert len(rec.requests) == 3
    rec = Recorder(httpx.Response(429), httpx.Response(200))
    await _storage(rec).upload("x", b"1", "application/json")
    assert len(rec.requests) == 2


async def test_retries_are_bounded_and_the_error_says_only_the_status(caplog):
    rec = Recorder(*(httpx.Response(502) for _ in range(5)))
    with pytest.raises(StorageError) as exc_info, caplog.at_level("WARNING"):
        await _storage(rec).upload("exports/secret-name.json", b"1", "x/y")
    assert len(rec.requests) == _http_retry.ATTEMPTS == 3
    err = exc_info.value
    assert (err.operation, err.status) == ("upload", 502)
    for text in (str(err), caplog.text):
        assert SERVICE_KEY not in text
        assert BASE not in text
        assert "secret-name" not in text


async def test_a_timeout_on_every_try_has_no_status():
    rec = Recorder(*(httpx.ReadTimeout("slow") for _ in range(3)))
    with pytest.raises(StorageError) as exc_info:
        await _storage(rec).delete(["x"])
    assert exc_info.value.status is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413])
async def test_a_4xx_is_final(status):
    rec = Recorder(httpx.Response(status), httpx.Response(200))
    with pytest.raises(StorageError) as exc_info:
        await _storage(rec).upload("x", b"1", "x/y")
    assert len(rec.requests) == 1
    assert exc_info.value.status == status


def test_every_call_has_a_timeout():
    client = ObjectStorage(BASE, SERVICE_KEY, "b")._client()
    assert client.timeout.read == object_storage.TIMEOUT_S


def test_from_settings_is_none_until_both_settings_are_set():
    with (
        patch.object(settings, "supabase_url", ""),
        patch.object(settings, "supabase_service_role_key", SERVICE_KEY),
    ):
        assert object_storage.from_settings() is None
    with (
        patch.object(settings, "supabase_url", BASE),
        patch.object(settings, "supabase_service_role_key", ""),
    ):
        assert object_storage.from_settings() is None
    with (
        patch.object(settings, "supabase_url", BASE),
        patch.object(settings, "supabase_service_role_key", SERVICE_KEY),
        patch.object(settings, "export_bucket", "my-bucket"),
    ):
        storage = object_storage.from_settings()
    assert storage is not None and storage.bucket == "my-bucket"
