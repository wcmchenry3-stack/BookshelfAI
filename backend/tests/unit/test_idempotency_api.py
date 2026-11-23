"""``Idempotency-Key`` handling around an endpoint (#685): the decorator and
the route class in ``app/api/_idempotency.py``, against an in-memory store.

The store itself (SQL, expiry, the lease) is in ``test_idempotency_store.py``;
the real endpoints on real Postgres (concurrency, the migration, the purge,
cascade, RLS) in ``tests/integration/test_idempotency_db.py``."""

import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import _idempotency as idem
from app.api._concurrency import VersionETagRoute
from app.core.config import settings
from app.core.limiter import limiter
from app.services import idempotency as store
from tests._idempotency_fake import FakeRow, install_fake_store

ALICE = uuid.UUID("00000000-0000-0000-0000-00000000a685")
BOB = uuid.UUID("00000000-0000-0000-0000-00000000b685")
# Built, not a literal: gitleaks reads a high-entropy `KEY = "..."` as a secret.
KEY = str(uuid.uuid5(uuid.NAMESPACE_URL, "bookshelfai-test-idempotency-key"))


@pytest.fixture
def fake_store(monkeypatch):
    return install_fake_store(monkeypatch)


# ── A probe app with keyed endpoints ─────────────────────────────────────────


class _ProbeRoute(idem.IdempotencyRoute, VersionETagRoute):
    pass


effects: list = []
probe_db = SimpleNamespace(rollback=AsyncMock())


def _user(x_user: str = Header(default=str(ALICE))):
    return SimpleNamespace(id=uuid.UUID(x_user))


def _db():
    return probe_db


router = APIRouter(route_class=_ProbeRoute)


@router.post("/things", status_code=201)
@limiter.limit("5/minute")
@idem.idempotent
async def create_thing(
    request: Request, body: dict, current_user=Depends(_user), db=Depends(_db)
):
    effects.append(body)
    return {"id": len(effects), "version": 1, "name": body.get("name")}


@router.patch("/things/{thing_id}")
@limiter.limit("5/minute")
@idem.idempotent
async def patch_thing(
    request: Request,
    thing_id: int,
    body: dict,
    current_user=Depends(_user),
    db=Depends(_db),
):
    effects.append((thing_id, body))
    return {"id": thing_id, "version": len(effects) + 1}


@router.delete("/things/{thing_id}", status_code=204)
@limiter.limit("5/minute")
@idem.idempotent
async def delete_thing(
    request: Request, thing_id: int, current_user=Depends(_user), db=Depends(_db)
) -> None:
    effects.append(("delete", thing_id))


@router.post("/refuse")
@limiter.limit("5/minute")
@idem.idempotent
async def refuse(request: Request, current_user=Depends(_user), db=Depends(_db)):
    effects.append("refuse")
    raise HTTPException(status_code=412, detail={"code": "x"}, headers={"ETag": '"7"'})


@router.post("/boom")
@limiter.limit("5/minute")
@idem.idempotent
async def boom(request: Request, current_user=Depends(_user), db=Depends(_db)):
    effects.append("boom")
    raise RuntimeError("server bug")


@router.post("/unavailable")
@limiter.limit("5/minute")
@idem.idempotent
async def unavailable(request: Request, current_user=Depends(_user), db=Depends(_db)):
    effects.append("unavailable")
    raise HTTPException(status_code=503, detail="busy")


@router.post("/big")
@limiter.limit("5/minute")
@idem.idempotent
async def big(request: Request, current_user=Depends(_user), db=Depends(_db)):
    effects.append("big")
    return {"blob": "x" * (idem.MAX_RESPONSE_BODY_BYTES + 1)}


probe = FastAPI()
probe.state.limiter = limiter
probe.include_router(router)


@pytest.fixture
def client():
    effects.clear()
    probe_db.rollback.reset_mock()
    return TestClient(probe, raise_server_exceptions=False)


def _keyed(key=KEY, **headers):
    return {idem.HEADER: key, **headers}


# ── No key, malformed keys, the kill switch ──────────────────────────────────


def test_a_missing_key_runs_the_endpoint_and_logs_the_user_id_only(
    client, fake_store, caplog
):
    caplog.set_level(logging.INFO, logger="app.api._idempotency")
    first = client.post("/things", json={"name": "a"})
    second = client.post("/things", json={"name": "a"})
    assert (first.status_code, second.status_code) == (201, 201)
    assert len(effects) == 2
    assert fake_store.claims == 0
    assert idem.REPLAYED_HEADER.lower() not in second.headers
    messages = [r.getMessage() for r in caplog.records if r.name == idem.__name__]
    assert messages == [f"idempotency: no key on create_thing user_id={ALICE}"] * 2


@pytest.mark.parametrize(
    "key",
    ["", " ", "a" * 256, "klüssel", "a b", "tab\there", "\x7f"],
    ids=["empty", "space", "too-long", "non-ascii", "inner-space", "tab", "del"],
)
def test_a_malformed_key_is_400_and_claims_nothing(client, fake_store, key):
    resp = client.post(
        "/things",
        json={"name": "a"},
        headers=[(idem.HEADER.encode(), key.encode("latin-1"))],
    )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {"detail": "invalid_idempotency_key"}
    assert effects == [] and fake_store.claims == 0


def test_boundary_keys_are_accepted(client, fake_store):
    for key in ("a", "~" * 255, "!#$%&'*+-.^_`|"):
        resp = client.post("/things", json={"name": key}, headers=_keyed(key))
        assert resp.status_code == 201, (key, resp.text)


def test_two_different_keys_are_400(client, fake_store):
    resp = client.post(
        "/things",
        json={},
        headers=[(b"idempotency-key", b"one"), (b"idempotency-key", b"two")],
    )
    assert resp.status_code == 400
    assert fake_store.claims == 0


def test_the_same_key_twice_is_one_key(client, fake_store):
    resp = client.post(
        "/things",
        json={},
        headers=[(b"idempotency-key", b"one"), (b"idempotency-key", b"one")],
    )
    assert resp.status_code == 201


def test_the_kill_switch_ignores_the_header(client, fake_store, monkeypatch):
    monkeypatch.setattr(settings, "idempotency_enabled", False)
    for _ in range(2):
        assert client.post("/things", json={}, headers=_keyed()).status_code == 201
    assert client.post("/things", json={}, headers=_keyed("")).status_code == 201
    assert len(effects) == 3 and fake_store.claims == 0


# ── Replay ───────────────────────────────────────────────────────────────────


def test_a_retry_replays_the_first_response_and_does_nothing_new(client, fake_store):
    first = client.post("/things", json={"name": "a"}, headers=_keyed())
    again = client.post("/things", json={"name": "a"}, headers=_keyed())
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() == {"id": 1, "version": 1, "name": "a"}
    assert again.headers["etag"] == first.headers["etag"] == '"1"'
    assert again.headers[idem.REPLAYED_HEADER] == "true"
    assert idem.REPLAYED_HEADER.lower() not in first.headers
    assert again.headers["content-type"] == "application/json"
    assert len(effects) == 1
    assert (fake_store.completes, fake_store.releases) == (1, 0)


def test_the_same_object_with_other_formatting_is_the_same_request(client, fake_store):
    client.post("/things", json={"name": "a", "n": 1}, headers=_keyed())
    again = client.post(
        "/things",
        content=b'{ "n": 1,\n  "name": "a" }',
        headers=_keyed(**{"Content-Type": "application/json"}),
    )
    assert again.status_code == 201
    assert again.headers[idem.REPLAYED_HEADER] == "true"
    assert len(effects) == 1


def test_a_204_is_replayed_without_a_body(client, fake_store):
    first = client.delete("/things/4", headers=_keyed())
    again = client.delete("/things/4", headers=_keyed())
    assert first.status_code == again.status_code == 204
    assert again.content == b""
    assert again.headers[idem.REPLAYED_HEADER] == "true"
    assert effects == [("delete", 4)]


def test_a_4xx_is_stored_and_replayed_after_a_rollback(client, fake_store):
    first = client.post("/refuse", headers=_keyed())
    again = client.post("/refuse", headers=_keyed())
    assert first.status_code == again.status_code == 412
    assert again.json() == first.json() == {"detail": {"code": "x"}}
    assert again.headers["etag"] == '"7"'
    assert again.headers[idem.REPLAYED_HEADER] == "true"
    assert effects == ["refuse"]
    probe_db.rollback.assert_awaited_once()


def test_another_users_key_is_never_replayed(client, fake_store):
    mine = client.post("/things", json={"name": "a"}, headers=_keyed())
    theirs = client.post(
        "/things", json={"name": "a"}, headers=_keyed(**{"X-User": str(BOB)})
    )
    assert mine.status_code == theirs.status_code == 201
    assert idem.REPLAYED_HEADER.lower() not in theirs.headers
    assert theirs.json()["id"] == 2
    assert len(effects) == 2


# ── Refusals: another request, still running ─────────────────────────────────


def test_the_key_on_a_different_body_is_422(client, fake_store):
    client.post("/things", json={"name": "a"}, headers=_keyed())
    resp = client.post("/things", json={"name": "b"}, headers=_keyed())
    assert resp.status_code == 422
    assert resp.json() == {"detail": "idempotency_key_reused"}
    assert len(effects) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("POST", "/things", {}), ("PATCH", "/things/1", {})),  # another endpoint
        (("PATCH", "/things/1", {}), ("PATCH", "/things/2", {})),  # another entry
        (  # another precondition
            ("PATCH", "/things/1", {"If-Match": '"1"'}),
            ("PATCH", "/things/1", {"If-Match": '"2"'}),
        ),
    ],
    ids=["endpoint", "entry", "if-match"],
)
def test_the_key_on_another_endpoint_entry_or_if_match_is_422(
    client, fake_store, first, second
):
    method, path, extra = first
    assert client.request(method, path, json={}, headers=_keyed(**extra)).is_success
    method, path, extra = second
    resp = client.request(method, path, json={}, headers=_keyed(**extra))
    assert resp.status_code == 422, resp.text
    assert len(effects) == 1


def test_a_duplicate_while_the_first_runs_is_409_with_retry_after(client, fake_store):
    pending = idem.request_hash(_request("POST", "/things"), b"{}")
    fake_store.rows[(ALICE, KEY)] = FakeRow(id=1, request_hash=pending)
    resp = client.post("/things", json={}, headers=_keyed())
    assert resp.status_code == 409
    assert resp.json() == {"detail": "idempotency_in_progress"}
    assert resp.headers["retry-after"] == "1"
    assert effects == []


# ── Failures release the key ─────────────────────────────────────────────────


def test_an_exception_releases_the_key_so_a_retry_runs_again(client, fake_store):
    assert client.post("/boom", headers=_keyed()).status_code == 500
    assert client.post("/boom", headers=_keyed()).status_code == 500
    assert effects == ["boom", "boom"]
    assert (fake_store.releases, fake_store.completes) == (2, 0)
    assert fake_store.rows == {}
    assert probe_db.rollback.await_count == 2


def test_a_5xx_http_exception_releases_the_key(client, fake_store):
    for _ in range(2):
        assert client.post("/unavailable", headers=_keyed()).status_code == 503
    assert effects == ["unavailable"] * 2
    assert fake_store.rows == {}


def test_a_body_over_the_cap_is_not_stored(client, fake_store, caplog):
    caplog.set_level(logging.WARNING, logger="app.api._idempotency")
    for _ in range(2):
        assert client.post("/big", headers=_keyed()).status_code == 200
    assert effects == ["big", "big"]
    assert fake_store.releases == 2
    assert f"too large) on big user_id={ALICE}" in caplog.text


def test_a_store_failure_never_changes_the_response(client, fake_store, caplog):
    fake_store.fail_complete = True
    resp = client.post("/things", json={"name": "a"}, headers=_keyed())
    assert resp.status_code == 201
    assert "could not settle the key on create_thing" in caplog.text
    assert KEY not in caplog.text


def test_a_store_failure_after_a_refusal_keeps_the_refusal(
    client, fake_store, monkeypatch
):
    monkeypatch.setattr(store, "complete", AsyncMock(side_effect=OSError("down")))
    assert client.post("/refuse", headers=_keyed()).status_code == 412


# ── Rate limit ───────────────────────────────────────────────────────────────


def test_a_replay_counts_against_the_rate_limit_and_a_429_claims_nothing(
    client, fake_store
):
    codes = [
        client.post("/things", json={}, headers=_keyed()).status_code for _ in range(6)
    ]
    assert codes == [201, 201, 201, 201, 201, 429]
    assert len(effects) == 1
    assert fake_store.claims == 5
    other = client.post("/things", json={}, headers=_keyed("fresh"))
    assert other.status_code == 429
    assert fake_store.claims == 5


# ── The decorator ────────────────────────────────────────────────────────────


def test_an_endpoint_without_the_parameters_is_refused_at_import():
    with pytest.raises(TypeError, match="lacks"):

        @idem.idempotent
        async def nameless(request, db):  # pragma: no cover - never runs
            return None


async def test_a_direct_call_without_a_request_runs_the_endpoint(fake_store):
    result = await create_thing.__wrapped__(
        request=SimpleNamespace(),
        body={"name": "direct"},
        current_user=SimpleNamespace(id=ALICE),
        db=probe_db,
    )
    assert result["name"] == "direct"
    assert fake_store.claims == 0


def test_the_decorator_marks_the_endpoint_through_the_limiter():
    assert idem.is_idempotent(create_thing)  # the limiter's wrapper
    assert idem.is_idempotent(create_thing.__wrapped__)
    assert not idem.is_idempotent(_db)


def test_a_write_route_without_the_decorator_is_refused_when_built():
    unkeyed = APIRouter(route_class=idem.IdempotencyRoute)

    async def plain(request: Request):  # pragma: no cover - never runs
        return None

    unkeyed.get("/read")(plain)  # reads need no key
    with pytest.raises(TypeError, match="/write lacks @idempotent"):
        unkeyed.post("/write")(plain)


def test_a_route_handler_without_a_claim_leaves_the_response(client, fake_store):
    assert client.post("/things", json={}).status_code == 201
    assert fake_store.completes == 0


# ── The request hash ─────────────────────────────────────────────────────────


def _request(method, path, query="", if_match=None):
    headers = [] if if_match is None else [(b"if-match", if_match.encode())]
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query.encode(),
            "headers": headers,
        }
    )


def test_the_hash_covers_method_path_query_if_match_and_body():
    base = idem.request_hash(_request("POST", "/a"), b'{"x":1}')
    assert len(base) == 64
    variants = [
        idem.request_hash(_request("PATCH", "/a"), b'{"x":1}'),
        idem.request_hash(_request("POST", "/b"), b'{"x":1}'),
        idem.request_hash(_request("POST", "/a", query="q=1"), b'{"x":1}'),
        idem.request_hash(_request("POST", "/a", if_match='"1"'), b'{"x":1}'),
        idem.request_hash(_request("POST", "/a"), b'{"x":2}'),
        idem.request_hash(_request("POST", "/a"), b""),
    ]
    assert base not in variants
    assert len(set(variants)) == len(variants)
    assert idem.request_hash(_request("POST", "/a"), b'{ "x" : 1 }') == base


def test_a_body_that_is_not_json_is_hashed_as_sent():
    assert idem._canonical_body(b"not json") == b"not json"
    assert idem._canonical_body(b"") == b""
    assert json.loads(idem._canonical_body(b'{"b":1,"a":2}')) == {"a": 2, "b": 1}


# ── The real app: Sentry, CORS and the write limits ──────────────────────────


def test_the_key_header_is_scrubbed_from_sentry_events():
    from app.core.sentry_context import build_event_scrubber

    event = {"request": {"headers": {"idempotency-key": KEY, "accept": "*/*"}}}
    build_event_scrubber().scrub_event(event)
    assert event["request"]["headers"]["idempotency-key"] != KEY
    assert event["request"]["headers"]["accept"] == "*/*"


def test_cors_allows_the_idempotency_key_header():
    from fastapi.middleware.cors import CORSMiddleware

    from app.main import app

    configured = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    origin = "https://app.example.test"
    cors_probe = FastAPI()
    cors_probe.add_middleware(
        CORSMiddleware, **{**configured.kwargs, "allow_origins": [origin]}
    )

    @cors_probe.post("/x")
    def x():  # pragma: no cover - only the preflight is sent
        return {}

    preflight = TestClient(cors_probe).options(
        "/x",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "idempotency-key, if-match",
        },
    )
    assert preflight.status_code == 200, preflight.text
    allowed = preflight.headers["access-control-allow-headers"].lower()
    assert "idempotency-key" in allowed


@pytest.mark.parametrize(
    "endpoint",
    [
        "user_books.add_to_wishlist",
        "user_books.add_unverified_to_wishlist",
        "user_books.add_purchased",
        "user_books.update_user_book",
        "user_books.delete_user_book",
        "user_books.restore_user_book",
        "users.update_preferences",
        "users.grant_ai_consent",
        "users.withdraw_ai_consent",
    ],
)
def test_every_keyed_endpoint_keeps_the_write_limit(endpoint):
    import app.main  # noqa: F401 - registers every router's limits

    (limit,) = limiter._route_limits[f"app.api.{endpoint}"]
    assert settings.rate_limit_writes.split("/")[0] in str(limit.limit)
    assert "minute" in str(limit.limit)
