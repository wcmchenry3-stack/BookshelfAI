"""Optimistic concurrency for library entries (#685): ``ETag`` / ``If-Match``.

Every ``user_books`` row has a ``version`` that the ORM bumps on each UPDATE
(``version_id_col``). A single-entry response carries it in the body and as
``ETag: "<version>"`` (set by :class:`VersionETagRoute`, the route class of
``app/api/user_books.py``). ``PATCH`` and ``DELETE /user-books/{id}`` take an
optional ``If-Match``:

- absent: accepted, so app builds from before #685 keep working; logged at
  info with the user id only;
- ``*``, or a list holding the entry's current ETag (strong ``"3"`` or weak
  ``W/"3"``; a proxy may weaken an ETag when it compresses a response):
  the write goes ahead;
- a list of our ETags none of which is current: ``412``
  ``{"detail": {"code": "version_mismatch", "current_version": N}}`` with the
  current ``ETag``; nothing is written;
- anything else (empty, unquoted, not a decimal version): ``400
  invalid_if_match``, checked before the database is touched.

The check is race-free: the endpoint reads the entry ``FOR UPDATE`` before it
compares, so a concurrent writer waits on the row lock and then sees the
bumped version (412). The ORM's ``UPDATE ... WHERE id = ? AND version = ?``
is a second guard; if it ever matches no row, :func:`stale_write_as_412`
turns the ``StaleDataError`` into the same 412.

An entry that is not the caller's (or not live) is a 404 before any version
check, so a 412 never reveals that another user's entry exists.
"""

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, Final

from fastapi import HTTPException, Request, Response, status
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

logger = logging.getLogger(__name__)

# 400 detail: If-Match is not ``*`` or a list of entry ETags.
IF_MATCH_INVALID: Final = "invalid_if_match"
# 412 detail code: the entry changed since the client read it.
VERSION_MISMATCH: Final = "version_mismatch"

# One entity tag of ours: an optional weak prefix and a quoted decimal
# version. Nine digits bound the integer (the column is a 32-bit INTEGER).
_ENTITY_TAG = re.compile(r'(?:W/)?"([0-9]{1,9})"')


class AnyVersion:
    """``If-Match: *``: any current version matches."""


ANY_VERSION: Final = AnyVersion()

IfMatch = AnyVersion | frozenset[int] | None


def etag(version: int) -> str:
    """The strong ``ETag`` of an entry at ``version``."""
    return f'"{version}"'


def parse_if_match(value: str | None) -> IfMatch:
    """``None`` when the header is absent, :data:`ANY_VERSION` for ``*``,
    else the set of versions it names. Raises 400 ``invalid_if_match`` for
    anything else."""
    if value is None:
        return None
    stripped = value.strip()
    if stripped == "*":
        return ANY_VERSION
    versions: set[int] = set()
    for part in stripped.split(","):
        match = _ENTITY_TAG.fullmatch(part.strip())
        if match is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=IF_MATCH_INVALID
            )
        versions.add(int(match.group(1)))
    return frozenset(versions)


def precondition_failed(current_version: int | None) -> HTTPException:
    """The 412 for a stale write. ``current_version`` is ``None`` only when
    the database refused the write (:func:`stale_write_as_412`) and the
    current version is not known."""
    headers = None if current_version is None else {"ETag": etag(current_version)}
    return HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail={"code": VERSION_MISMATCH, "current_version": current_version},
        headers=headers,
    )


def require_version(
    wanted: IfMatch, current_version: int, *, user_id: uuid.UUID, action: str
) -> None:
    """Refuse the write with 412 unless ``wanted`` (the parsed If-Match)
    matches ``current_version``, which the caller read under a row lock. A
    missing If-Match is accepted and logged (user id only)."""
    if wanted is None:
        logger.info("if_match: missing on %s user_id=%s", action, user_id)
        return
    if isinstance(wanted, AnyVersion) or current_version in wanted:
        return
    logger.info("if_match: stale %s refused user_id=%s", action, user_id)
    raise precondition_failed(current_version)


def add_version_etag(response: Response) -> None:
    """Set ``ETag`` from the ``version`` of a single-entry JSON response
    (a 200 / 201 whose body is an object with an integer ``version``). Lists,
    204s, errors and responses that already have an ETag are left alone."""
    if response.status_code not in (status.HTTP_200_OK, status.HTTP_201_CREATED):
        return
    if "etag" in response.headers or response.media_type != "application/json":
        return
    body = getattr(response, "body", b"")
    if not body.startswith(b"{"):
        return  # a list (GET /user-books) or no buffered body
    version = json.loads(body).get("version")
    if type(version) is int:
        response.headers["ETag"] = etag(version)


class VersionETagRoute(APIRoute):
    """A route whose single-entry responses carry the entry's ``ETag``
    (#685), so every endpoint that returns one entry (adds, ``PATCH``,
    restore) sends it without setting it by hand."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def route_handler(request: Request) -> Response:
            response = await handler(request)
            add_version_etag(response)
            return response

        return route_handler


@asynccontextmanager
async def stale_write_as_412(db: AsyncSession) -> AsyncIterator[None]:
    """Turn the ORM's ``StaleDataError`` (its versioned UPDATE matched no
    row) raised inside the block into a 412, after rolling back."""
    try:
        yield
    except StaleDataError:
        await db.rollback()
        logger.info("if_match: versioned update matched no row")
        raise precondition_failed(None) from None
