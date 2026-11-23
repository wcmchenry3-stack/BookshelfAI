"""Optimistic concurrency helpers (#685): ``If-Match`` parsing, the version
check, the 412, the ``ETag`` route and the ``StaleDataError`` mapping.

The flows on real Postgres (concurrent writers, the migration) are in
``tests/integration/test_library_entry_version_db.py``."""

import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm.exc import StaleDataError

from app.api import _concurrency as cc
from app.api import user_books as user_books_api
from app.core.config import settings
from app.core.limiter import limiter
from app.main import app
from app.models.user_book import UserBook
from app.models.user_custom_book import UserCustomBook

USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000685")


# ── If-Match parsing ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "versions"),
    [
        ('"3"', {3}),
        ('W/"3"', {3}),
        ('  "12"  ', {12}),
        ('"1", W/"4" ,"7"', {1, 4, 7}),
        ('"999999999"', {999_999_999}),
        # RFC 9110 lists may hold empty elements (a proxy's trailing comma).
        ('"1",', {1}),
        ('"3", ,"4"', {3, 4}),
    ],
)
def test_entity_tags_are_parsed(header, versions):
    assert cc.parse_if_match(header) == frozenset(versions)


def test_absent_and_star():
    assert cc.parse_if_match(None) is None
    assert cc.parse_if_match("*") is cc.ANY_VERSION
    assert cc.parse_if_match(" * ") is cc.ANY_VERSION


@pytest.mark.parametrize(
    "header",
    [
        "",
        " ",
        "3",
        '"3',
        '3"',
        "'3'",
        "W/3",
        'w/"3"',
        '"abc"',
        '"-1"',
        '"3.0"',
        '""',
        '"1234567890"',  # beyond the 32-bit column
        ",",
        " , ,",
        '"1" "2"',
        '*, "1"',
    ],
)
def test_malformed_if_match_is_400(header):
    with pytest.raises(HTTPException) as exc:
        cc.parse_if_match(header)
    assert exc.value.status_code == 400
    assert exc.value.detail == cc.IF_MATCH_INVALID == "invalid_if_match"


# ── The version check ────────────────────────────────────────────────────────


def test_matching_version_passes():
    cc.require_version(frozenset({2}), 2, user_id=USER_ID, action="update")
    cc.require_version(frozenset({1, 2}), 2, user_id=USER_ID, action="update")
    cc.require_version(cc.ANY_VERSION, 9, user_id=USER_ID, action="delete")


def test_stale_version_is_412_with_the_current_version():
    with pytest.raises(HTTPException) as exc:
        cc.require_version(frozenset({1}), 2, user_id=USER_ID, action="update")
    assert exc.value.status_code == 412
    assert exc.value.detail == {"code": "version_mismatch", "current_version": 2}
    assert exc.value.headers == {"ETag": '"2"'}


def test_missing_if_match_is_accepted_and_logged_with_the_user_id_only(caplog):
    caplog.set_level(logging.INFO, logger="app.api._concurrency")
    cc.require_version(None, 5, user_id=USER_ID, action="delete")
    (record,) = [r for r in caplog.records if r.name == "app.api._concurrency"]
    assert record.levelno == logging.INFO
    assert record.getMessage() == f"if_match: missing on delete user_id={USER_ID}"


def test_unknown_current_version_has_no_etag():
    exc = cc.precondition_failed(None)
    assert exc.status_code == 412
    assert exc.detail == {"code": "version_mismatch", "current_version": None}
    assert exc.headers is None


def test_etag_is_strong_and_quoted():
    assert cc.etag(1) == '"1"'


# ── StaleDataError ───────────────────────────────────────────────────────────


async def test_stale_data_error_becomes_412_after_a_rollback():
    db = SimpleNamespace(rollback=AsyncMock())
    with pytest.raises(HTTPException) as exc:
        async with cc.stale_write_as_412(db):
            raise StaleDataError("UPDATE matched 0 rows")
    assert exc.value.status_code == 412
    assert exc.value.detail["current_version"] is None
    db.rollback.assert_awaited_once()


async def test_other_errors_pass_through_untouched():
    db = SimpleNamespace(rollback=AsyncMock())
    with pytest.raises(ValueError):
        async with cc.stale_write_as_412(db):
            raise ValueError("boom")
    db.rollback.assert_not_awaited()


async def test_patch_maps_a_stale_flush_to_412(monkeypatch):
    """The ORM's versioned UPDATE matching no row (a write the row lock did
    not serialise) answers 412, not 500."""
    entry = UserBook(id=uuid.uuid4(), user_id=USER_ID, status="read")
    result = SimpleNamespace(scalar_one_or_none=lambda: entry)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        flush=AsyncMock(side_effect=StaleDataError("0 rows")),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        user_books_api.library_events, "record_change", AsyncMock(return_value=None)
    )
    with pytest.raises(HTTPException) as exc:
        await user_books_api.update_user_book.__wrapped__(
            request=SimpleNamespace(),
            user_book_id=entry.id,
            req=user_books_api.UserBookUpdate(notes="x"),
            if_match='"1"',
            current_user=SimpleNamespace(id=USER_ID),
            db=db,
        )
    assert exc.value.status_code == 412
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


async def test_delete_maps_a_stale_flush_to_412(monkeypatch):
    entry = UserBook(id=uuid.uuid4(), user_id=USER_ID, status="read")
    entry.custom_book = None
    result = SimpleNamespace(scalar_one_or_none=lambda: entry)
    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(user_books_api, "lock_user", AsyncMock())
    monkeypatch.setattr(
        user_books_api.library_events,
        "record_change",
        AsyncMock(side_effect=StaleDataError("0 rows")),
    )
    with pytest.raises(HTTPException) as exc:
        await user_books_api.delete_user_book.__wrapped__(
            request=SimpleNamespace(),
            user_book_id=entry.id,
            current_user=SimpleNamespace(id=USER_ID),
            db=db,
        )
    assert exc.value.status_code == 412
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


async def test_a_bad_if_match_is_refused_before_the_database():
    db = SimpleNamespace(execute=AsyncMock())
    for call, extra in (
        (user_books_api.update_user_book, {"req": user_books_api.UserBookUpdate()}),
        (user_books_api.delete_user_book, {}),
    ):
        with pytest.raises(HTTPException) as exc:
            await call.__wrapped__(
                request=SimpleNamespace(),
                user_book_id=uuid.uuid4(),
                if_match="3",
                current_user=SimpleNamespace(id=USER_ID),
                db=db,
                **extra,
            )
        assert exc.value.status_code == 400
    db.execute.assert_not_awaited()


# ── The ETag route ───────────────────────────────────────────────────────────


def _json(body, status: int = 200) -> Response:
    return JSONResponse(body, status_code=status)


@pytest.mark.parametrize("status", [200, 201])
def test_single_entry_response_gets_its_etag(status):
    response = _json({"id": "x", "version": 4}, status)
    cc.add_version_etag(response)
    assert response.headers["etag"] == '"4"'


@pytest.mark.parametrize(
    "response",
    [
        _json([{"id": "x", "version": 4}]),  # a list
        _json({"id": "x"}),  # no version
        _json({"version": "4"}),  # not an integer
        _json({"version": True}),  # bool is not a version
        _json({"version": 4}, 202),
        Response(status_code=204),
        Response(b'{"version": 4}', media_type="text/plain"),
    ],
)
def test_other_responses_get_no_etag(response):
    cc.add_version_etag(response)
    assert "etag" not in response.headers


def test_an_etag_already_set_is_kept():
    response = _json({"version": 4})
    response.headers["ETag"] = '"9"'
    cc.add_version_etag(response)
    assert response.headers["etag"] == '"9"'


def test_route_class_sets_the_etag_and_errors_keep_theirs():
    probe = FastAPI()
    from fastapi import APIRouter

    router = APIRouter(route_class=cc.VersionETagRoute)

    @router.get("/one")
    def one():
        return {"version": 7}

    @router.get("/many")
    def many():
        return [{"version": 7}]

    @router.get("/stale")
    def stale():
        raise cc.precondition_failed(8)

    probe.include_router(router)
    client = TestClient(probe)
    assert client.get("/one").headers["etag"] == '"7"'
    assert "etag" not in client.get("/many").headers
    refused = client.get("/stale")
    assert refused.status_code == 412
    assert refused.headers["etag"] == '"8"'
    assert json.loads(refused.text)["detail"] == {
        "code": "version_mismatch",
        "current_version": 8,
    }


def test_library_routes_use_the_etag_route():
    paths = {"/wishlist", "/wishlist/unverified", "/purchased"}
    paths |= {"/user-books/{user_book_id}", "/user-books/{user_book_id}/restore"}
    routes = [r for r in user_books_api.router.routes if r.path in paths]
    assert {r.path for r in routes} == paths
    assert all(isinstance(r, cc.VersionETagRoute) for r in routes)


# ── Models, limits, CORS ─────────────────────────────────────────────────────


@pytest.mark.parametrize("model", [UserBook, UserCustomBook])
def test_models_are_versioned_and_start_at_one(model):
    assert model.__mapper__.version_id_col is model.__table__.c.version
    assert model().version == 1
    assert model(version=4).version == 4


@pytest.mark.parametrize("endpoint", ["update_user_book", "delete_user_book"])
def test_if_match_endpoints_keep_the_write_limit(endpoint):
    limits = limiter._route_limits[f"app.api.user_books.{endpoint}"]
    assert [str(lim.limit) for lim in limits] == [
        str(lim.limit)
        for lim in limiter._route_limits["app.api.user_books.restore_user_book"]
    ]
    assert settings.rate_limit_writes.split("/")[0] in str(limits[0].limit)


def test_cors_allows_if_match_and_exposes_etag():
    configured = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    origin = "https://app.example.test"
    probe = FastAPI()
    probe.add_middleware(
        CORSMiddleware, **{**configured.kwargs, "allow_origins": [origin]}
    )

    @probe.patch("/x")
    def x():
        return {"version": 1}

    client = TestClient(probe)
    preflight = client.options(
        "/x",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PATCH",
            "Access-Control-Request-Headers": "if-match, authorization",
        },
    )
    assert preflight.status_code == 200, preflight.text
    allowed = preflight.headers["access-control-allow-headers"].lower()
    assert "if-match" in allowed
    exposed = client.patch("/x", headers={"Origin": origin}).headers[
        "access-control-expose-headers"
    ]
    assert "etag" in exposed.lower()
