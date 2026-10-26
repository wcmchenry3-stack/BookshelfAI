"""Request body size limit (#557): RequestSizeLimitMiddleware in app/main.py.

The middleware counts the bytes of the ``http.request`` messages, so the cap
holds for ``Transfer-Encoding: chunked`` uploads (no Content-Length) as well
as for declared lengths. The scan upload route is capped at
MAX_SCAN_REQUEST_BYTES (the 5 MB image limit plus multipart overhead), every
other route at 10 MB.

End-to-end tests go through the real ``app`` with
``httpx.AsyncClient(transport=ASGITransport(app=app))`` and an async
generator body, which httpx sends chunked. Unit tests drive the middleware
directly to check what it reads.

pytest -m security -v tests/security/test_request_size_limit.py
"""

import io
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from PIL import Image

from app.ai.providers.base import ProviderResult

CHUNK = 64 * 1024


def _main():
    # app.main is reloaded by test_environment_hardening, so it is imported
    # inside each test: the ``app`` and the patch targets must be the same
    # module generation.
    import app.main

    return app.main


@pytest.fixture(autouse=True)
def _no_scan_request_count():
    """/scan counts each request with a DB upsert (#586), which the fake DB
    here cannot run. Stubbed, and asserted on where it matters."""
    with patch("app.api.scan._count_scan_request", new=AsyncMock()) as count:
        yield count


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def _multipart(file_bytes: bytes, boundary: str) -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + file_bytes
        + f"\r\n--{boundary}--\r\n".encode()
    )


async def _chunked(body: bytes):
    for i in range(0, len(body), CHUNK):
        yield body[i : i + CHUNK]


@pytest.fixture
def scan_app():
    """The real app with auth and DB faked and every cost-bearing step of the
    scan handler mocked, so a test can assert none of them ran."""
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db
    from app.models.user import User

    app = _main().app

    async def _fake_db():
        yield AsyncMock()

    user = User(id=uuid.UUID(int=557), email="u@example.com")
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _fake_db
    with (
        patch("app.api.scan._reserve_scan_quota", new=AsyncMock()) as reserve,
        patch("app.api.scan._refund_scan_quota", new=AsyncMock()),
        patch("app.api.scan._release_scan_lease", new=AsyncMock()),
        patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
        patch("app.api.scan.EnrichmentService"),
        patch("app.api.scan.DeduplicationService"),
    ):
        provider_cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        yield app, reserve, provider_cls
    app.dependency_overrides.clear()


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


# ---------------------------------------------------------------------------
# End to end through the real app
# ---------------------------------------------------------------------------


@pytest.mark.security
class TestChunkedUploadsThroughTheApp:
    async def test_chunked_scan_upload_over_the_scan_cap_is_413_and_handler_never_runs(
        self, scan_app, _no_scan_request_count
    ):
        from app.api.scan import MAX_SCAN_REQUEST_BYTES, SCAN_UPLOAD_PATH

        app, reserve, provider_cls = scan_app
        boundary = "b557"
        body = _multipart(_jpeg() + b"x" * MAX_SCAN_REQUEST_BYTES, boundary)
        settings_spy = AsyncMock()
        with patch("app.api.scan.get_ai_settings", settings_spy):
            async with _client(app) as client:
                resp = await client.post(
                    SCAN_UPLOAD_PATH,
                    content=_chunked(body),
                    headers={
                        "content-type": f"multipart/form-data; boundary={boundary}"
                    },
                )

        assert resp.status_code == 413
        assert resp.json() == {"detail": "Request body too large"}
        # The handler never ran on the truncated body: its first step (the
        # kill-switch settings read), the request count, the quota
        # reservation and the AI provider were all untouched.
        settings_spy.assert_not_awaited()
        _no_scan_request_count.assert_not_awaited()
        reserve.assert_not_awaited()
        provider_cls.assert_not_called()

    async def test_chunked_body_over_10mb_on_another_route_is_413(self):
        too_big = _main().RequestSizeLimitMiddleware.MAX_BODY_SIZE + 1
        async with _client(_main().app) as client:
            resp = await client.post(
                "/auth/logout",
                content=_chunked(b"{" + b" " * (too_big - 2) + b"}"),
                headers={"content-type": "application/json"},
            )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "Request body too large"}

    async def test_chunked_scan_upload_just_under_the_cap_reaches_the_handler(
        self, scan_app
    ):
        """Just under the request cap but over the 5 MB image limit: the
        handler's own streaming check answers, so the middleware let it by."""
        from app.api.scan import MAX_SCAN_REQUEST_BYTES, SCAN_UPLOAD_PATH

        app, _reserve, provider_cls = scan_app
        boundary = "b557"
        overhead = len(_multipart(b"", boundary))
        jpeg = _jpeg()
        body = _multipart(
            jpeg + b"x" * (MAX_SCAN_REQUEST_BYTES - overhead - len(jpeg) - 1),
            boundary,
        )
        assert len(body) == MAX_SCAN_REQUEST_BYTES - 1
        async with _client(app) as client:
            resp = await client.post(
                SCAN_UPLOAD_PATH,
                content=_chunked(body),
                headers={"content-type": f"multipart/form-data; boundary={boundary}"},
            )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "Image must be 5MB or smaller"}
        provider_cls.assert_not_called()

    @pytest.mark.parametrize(
        "headers",
        [
            [(b"content-length", b"abc")],
            [(b"content-length", b"-1")],
            [(b"content-length", b"+5")],
            [(b"content-length", b"")],
            [(b"content-length", b"1"), (b"content-length", b"2")],
            [(b"content-length", b"1, 2")],
        ],
        ids=["non-numeric", "negative", "signed", "empty", "two-headers", "list"],
    )
    async def test_malformed_content_length_is_400(self, headers):
        status, body = await _raw_request(_main().app, "GET", "/health", headers)
        assert status == 400
        assert body == b'{"detail": "Invalid Content-Length header"}'

    async def test_identical_repeated_content_length_is_accepted(self):
        headers = [(b"content-length", b"0"), (b"content-length", b"0")]
        status, _ = await _raw_request(_main().app, "GET", "/health", headers)
        assert status == 200


async def _raw_request(app, method, path, headers):
    """Call the ASGI app with exactly these headers (httpx would rewrite a
    bad Content-Length). Returns (status, body)."""
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return start["status"], body


# ---------------------------------------------------------------------------
# Unit level: the middleware alone around a fake app
# ---------------------------------------------------------------------------


def _http_scope(path: str, headers=()) -> dict:
    return {"type": "http", "method": "POST", "path": path, "headers": list(headers)}


def _receiver(chunks: list[bytes]):
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    receive = AsyncMock(side_effect=messages)
    return receive


class _Recorder:
    def __init__(self):
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def statuses(self) -> list[int]:
        return [
            m["status"] for m in self.messages if m["type"] == "http.response.start"
        ]


async def _body_reading_app(scope, receive, send):
    """Reads the whole body, then answers 200 with the byte count."""
    total = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"cut off"})
            return
        total += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": str(total).encode()})


def _middleware(app=_body_reading_app):
    return _main().RequestSizeLimitMiddleware(app)


@pytest.mark.security
class TestRequestSizeLimitMiddlewareUnit:
    @pytest.mark.parametrize("path_kind", ["scan", "other"])
    async def test_declared_length_over_the_cap_is_413_without_reading_the_body(
        self, path_kind
    ):
        from app.api.scan import SCAN_UPLOAD_PATH

        mw_cls = _main().RequestSizeLimitMiddleware
        path = SCAN_UPLOAD_PATH if path_kind == "scan" else "/auth/logout"
        cap = mw_cls.limit_for(path)
        receive = AsyncMock(
            return_value={"type": "http.request", "body": b"", "more_body": False}
        )
        send = _Recorder()
        await _middleware()(
            _http_scope(path, [(b"content-length", str(cap + 1).encode())]),
            receive,
            send,
        )
        assert send.statuses == [413]
        receive.assert_not_called()

    def test_scan_cap_is_derived_from_the_image_limit(self):
        from app.api.scan import MAX_IMAGE_BYTES, SCAN_UPLOAD_PATH

        mw_cls = _main().RequestSizeLimitMiddleware
        scan_cap = mw_cls.limit_for(SCAN_UPLOAD_PATH)
        assert MAX_IMAGE_BYTES < scan_cap <= MAX_IMAGE_BYTES + 1024 * 1024
        assert mw_cls.limit_for(SCAN_UPLOAD_PATH + "/") == scan_cap
        assert mw_cls.limit_for("/books/search") == 10 * 1024 * 1024
        # The cap is on the real scan route's path.
        assert "post" in _main().app.openapi()["paths"][SCAN_UPLOAD_PATH]

    @pytest.mark.parametrize("path_kind", ["scan", "other"])
    async def test_body_exactly_at_the_cap_with_content_length_passes(self, path_kind):
        from app.api.scan import SCAN_UPLOAD_PATH

        path = SCAN_UPLOAD_PATH if path_kind == "scan" else "/auth/logout"
        cap = _main().RequestSizeLimitMiddleware.limit_for(path)
        chunks = [b"x" * CHUNK] * (cap // CHUNK) + [b"x" * (cap % CHUNK)]
        send = _Recorder()
        await _middleware()(
            _http_scope(path, [(b"content-length", str(cap).encode())]),
            _receiver(chunks),
            send,
        )
        assert send.statuses == [200]
        assert send.messages[-1]["body"] == str(cap).encode()

    async def test_chunked_body_just_under_the_cap_passes(self):
        from app.api.scan import SCAN_UPLOAD_PATH

        cap = _main().RequestSizeLimitMiddleware.limit_for(SCAN_UPLOAD_PATH)
        chunks = [b"x" * (cap - 1)]
        send = _Recorder()
        await _middleware()(_http_scope(SCAN_UPLOAD_PATH), _receiver(chunks), send)
        assert send.statuses == [200]

    async def test_chunked_body_over_the_cap_is_413_and_app_response_is_dropped(
        self,
    ):
        from app.api.scan import SCAN_UPLOAD_PATH

        cap = _main().RequestSizeLimitMiddleware.limit_for(SCAN_UPLOAD_PATH)
        chunks = [b"x" * cap, b"x", b"never read"]
        receive = _receiver(chunks)
        send = _Recorder()
        await _middleware()(_http_scope(SCAN_UPLOAD_PATH), receive, send)
        # Only the middleware's 413: the fake app's 400 for the cut-off body
        # was suppressed, and the third chunk was never pulled.
        assert send.statuses == [413]
        assert send.messages[-1]["body"] == b'{"detail": "Request body too large"}'
        assert receive.await_count == 2

    async def test_app_exception_after_the_cut_off_still_gives_413(self):
        async def raising_app(scope, receive, send):
            while (await receive())["type"] != "http.disconnect":
                pass
            raise RuntimeError("client disconnected")

        send = _Recorder()
        await _middleware(raising_app)(
            _http_scope("/x"), _receiver([b"x" * (10 * 1024 * 1024 + 1)]), send
        )
        assert send.statuses == [413]

    async def test_app_exception_without_overflow_propagates(self):
        async def raising_app(scope, receive, send):
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await _middleware(raising_app)(
                _http_scope("/x"), _receiver([b""]), _Recorder()
            )

    async def test_response_started_before_the_overflow_is_left_alone(self):
        async def early_response_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while (await receive())["type"] != "http.disconnect":
                pass
            await send({"type": "http.response.body", "body": b"done"})

        send = _Recorder()
        await _middleware(early_response_app)(
            _http_scope("/x"), _receiver([b"x" * (10 * 1024 * 1024 + 1)]), send
        )
        # No second response start can be sent once one has gone out.
        assert send.statuses == [200]
        assert send.messages[-1]["body"] == b"done"

    @pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
    async def test_non_http_scopes_pass_straight_through(self, scope_type):
        seen = {}

        async def inner(scope, receive, send):
            seen["scope"] = scope
            seen["receive"] = receive
            seen["send"] = send

        receive, send = AsyncMock(), AsyncMock()
        # A huge declared length would be refused on an HTTP scope.
        scope = {"type": scope_type, "headers": [(b"content-length", b"abc")]}
        await _main().RequestSizeLimitMiddleware(inner)(scope, receive, send)
        assert seen == {"scope": scope, "receive": receive, "send": send}
