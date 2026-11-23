"""``Idempotency-Key`` on library and account writes (#685).

A client may send ``Idempotency-Key: <key>`` (1-255 visible ASCII
characters, a fresh UUID per user action) on a write and repeat it on a
retry. Within 24 hours a repeat of the same request (method, path,
``If-Match`` and JSON body) gets the first response back, with
``Idempotent-Replayed: true``, and the endpoint does not run again. The
same key on a different request is ``422 idempotency_key_reused``; a repeat
while the first is still running is ``409 idempotency_in_progress`` with
``Retry-After: 1``; a malformed key is ``400 invalid_idempotency_key``.
Keys are per user. A missing key keeps the old behaviour (logged at info,
user id only); ``settings.idempotency_enabled`` off ignores the header.

Two pieces, both needed on a keyed route:

- :func:`idempotent`, the decorator under ``@limiter.limit``: validates the
  key, claims it (``app/services/idempotency.py``) or replays, and runs the
  endpoint. Under the limiter, so a replay counts against the rate limit
  and a ``429`` never claims a key. An ``HTTPException`` 4xx from the
  endpoint is stored and replayed (Stripe's convention: the same request
  gets the same answer), after a rollback of anything the endpoint left
  uncommitted; a 5xx, a ``429`` or any other exception releases the key, so
  a retry runs again.
- :class:`IdempotencyRoute`, the router's route class: stores a successful
  response once FastAPI has serialised it (status, body, ``ETag``,
  ``Location``). Put it before ``VersionETagRoute`` in the bases, so the
  stored headers include the entry's ``ETag``. A replay returns the stored
  response as it was, even if the entry's version has moved on since.

``tests/security/test_idempotency_coverage.py`` checks that every write on
the library and account routers has both. Never logs a key or a body.
"""

import functools
import hashlib
import inspect
import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Final

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api._routes import AfterResponseRoute
from app.core.config import settings
from app.models.idempotency_key import MAX_RESPONSE_BODY_BYTES
from app.services import idempotency
from app.services.idempotency import StoredResponse

logger = logging.getLogger(__name__)

HEADER: Final = "Idempotency-Key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"
# 400: the key is empty, too long or not visible ASCII.
KEY_INVALID: Final = "invalid_idempotency_key"
# 422: the key was used for a different request.
KEY_REUSED: Final = "idempotency_key_reused"
# 409: the first request with this key is still running.
KEY_IN_PROGRESS: Final = "idempotency_in_progress"
_IN_PROGRESS_RETRY_AFTER: Final = "1"

_KEY = re.compile(r"[\x21-\x7e]{1,255}")
# Set on request.state by the decorator, read by the route class.
_CLAIM_STATE: Final = "idempotency_claim"
# Parameters the decorated endpoint must have, by name.
_REQUIRED_PARAMS: Final = ("request", "current_user", "db")
# Set on the decorator's wrapper; read through any later wrappers.
_MARKER: Final = "__idempotent__"
_WRITE_METHODS: Final = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def parse_key(request: Request) -> str | None:
    """The request's key, ``None`` when it sends none. Raises 400
    ``invalid_idempotency_key`` for a malformed key or two different ones."""
    values = set(request.headers.getlist(HEADER))
    if not values:
        return None
    if len(values) > 1 or _KEY.fullmatch(value := values.pop()) is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=KEY_INVALID)
    return value


def _canonical_body(body: bytes) -> bytes:
    """A JSON body re-serialised with sorted keys and no spaces, so the same
    object sent with other formatting hashes the same; anything else as is."""
    if not body:
        return b""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    return json.dumps(
        parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def request_hash(request: Request, body: bytes) -> str:
    """SHA-256 of what makes two requests the same: method, concrete path
    (so the same key on another entry or another endpoint is a different
    request), query, ``If-Match`` and the canonical body."""
    digest = hashlib.sha256()
    parts = (
        request.method,
        request.url.path,
        request.url.query,
        request.headers.get("if-match", ""),
    )
    for part in parts:
        digest.update(f"{len(part)}:{part}\n".encode())
    digest.update(_canonical_body(body))
    return digest.hexdigest()


def _replay(stored: StoredResponse) -> Response:
    headers = {**stored.headers, REPLAYED_HEADER: "true"}
    if stored.body is None:
        return Response(status_code=stored.status, headers=headers)
    return Response(
        content=stored.body,
        status_code=stored.status,
        headers=headers,
        media_type="application/json",
    )


def _is_stored(status_code: int) -> bool:
    """A response replayed to a retry: success and the endpoint's 4xx
    refusals. A 5xx or a 429 releases the key instead."""
    return status_code < 500 and status_code != status.HTTP_429_TOO_MANY_REQUESTS


def _stored_from(response: Response) -> StoredResponse | None:
    """The replayable part of ``response``; ``None`` when its body is over
    the cap or not buffered (it is then not stored)."""
    body = getattr(response, "body", None)
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BODY_BYTES:
        return None
    headers = {
        name: response.headers[name]
        for name in idempotency.REPLAYED_HEADERS
        if name in response.headers
    }
    return StoredResponse(
        status=response.status_code,
        body=body.decode() if body else None,
        headers=headers,
    )


class _Claim:
    """A key this request holds (a pending row): settled once, by the
    decorator when the endpoint raises, else by the route class."""

    def __init__(
        self, db: AsyncSession, record_id: int, user_id: uuid.UUID, action: str
    ) -> None:
        self.db = db
        self.record_id = record_id
        self.user_id = user_id
        self.action = action

    async def settle(self, response: Response | None, *, rollback: bool) -> None:
        """Store ``response`` if it is replayable, else release the key.
        ``rollback`` first discards what a failed endpoint left uncommitted.
        Never raises: the client's response must not depend on this; a row
        left pending is free again after ``PENDING_LEASE``."""
        stored = None
        if response is not None and _is_stored(response.status_code):
            stored = _stored_from(response)
            if stored is None:
                logger.warning(
                    "idempotency: response not stored (too large) on %s user_id=%s",
                    self.action,
                    self.user_id,
                )
        try:
            if rollback:
                await self.db.rollback()
            if stored is None:
                await idempotency.release(self.db, self.record_id)
            else:
                await idempotency.complete(self.db, self.record_id, stored)
        except Exception:
            logger.exception(
                "idempotency: could not settle the key on %s user_id=%s",
                self.action,
                self.user_id,
            )


def _exception_response(exc: HTTPException) -> Response:
    """The response FastAPI's handler sends for ``exc``."""
    return JSONResponse({"detail": exc.detail}, exc.status_code, exc.headers)


def idempotent(
    func: Callable[..., Awaitable[Any]],
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """Honour ``Idempotency-Key`` on this endpoint. Goes directly under
    ``@limiter.limit``; the endpoint takes ``request``, ``current_user`` and
    ``db`` by those names, and its router uses :class:`IdempotencyRoute`."""
    signature = inspect.signature(func)
    missing = [name for name in _REQUIRED_PARAMS if name not in signature.parameters]
    if missing:
        raise TypeError(f"@idempotent endpoint {func.__name__} lacks {missing}")
    action = func.__name__

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        arguments = signature.bind_partial(*args, **kwargs).arguments
        request = arguments.get("request")
        # Off, or called directly rather than over HTTP (no headers to
        # honour): the endpoint as it was.
        if not settings.idempotency_enabled or not isinstance(request, Request):
            return await func(*args, **kwargs)
        user_id = arguments["current_user"].id
        key = parse_key(request)
        if key is None:
            logger.info("idempotency: no key on %s user_id=%s", action, user_id)
            return await func(*args, **kwargs)
        db: AsyncSession = arguments["db"]
        digest = request_hash(request, await request.body())
        try:
            claimed = await idempotency.claim(db, user_id, key, digest)
        except idempotency.KeyReusedError:
            logger.info("idempotency: key reused on %s user_id=%s", action, user_id)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=KEY_REUSED
            ) from None
        except idempotency.KeyInProgressError:
            logger.info("idempotency: in progress on %s user_id=%s", action, user_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=KEY_IN_PROGRESS,
                headers={"Retry-After": _IN_PROGRESS_RETRY_AFTER},
            ) from None
        if isinstance(claimed, StoredResponse):
            logger.info("idempotency: replayed %s user_id=%s", action, user_id)
            return _replay(claimed)
        claim = _Claim(db, claimed, user_id, action)
        setattr(request.state, _CLAIM_STATE, claim)
        try:
            return await func(*args, **kwargs)
        except HTTPException as exc:
            await claim.settle(_exception_response(exc), rollback=True)
            raise
        except Exception:
            await claim.settle(None, rollback=True)
            raise

    setattr(wrapper, _MARKER, True)
    return wrapper


def is_idempotent(endpoint: Callable[..., Any]) -> bool:
    """Whether ``endpoint`` (or a function it wraps, e.g. under the
    limiter's wrapper) is an :func:`idempotent` endpoint."""
    fn: Any = endpoint
    while fn is not None:
        if getattr(fn, _MARKER, False):
            return True
        fn = getattr(fn, "__wrapped__", None)
    return False


class IdempotencyRoute(AfterResponseRoute):
    """Stores the serialised response of an :func:`idempotent` endpoint that
    returned normally (its exceptions are settled by the decorator). Every
    write route on such a router must be :func:`idempotent`, checked when
    the route is built, so a new one cannot silently ignore the header."""

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        super().__init__(path, endpoint, **kwargs)
        if self.methods & _WRITE_METHODS and not is_idempotent(endpoint):
            raise TypeError(f"write route {path} lacks @idempotent")

    async def after_response(self, request: Request, response: Response) -> None:
        # The later bases first (VersionETagRoute's ETag), then store.
        await super().after_response(request, response)
        claim = getattr(request.state, _CLAIM_STATE, None)
        if isinstance(claim, _Claim):
            await claim.settle(response, rollback=False)
