"""The HTTP request and result shaping shared by the book lookup clients
(``google_books``, ``open_library``, #814).

Both clients make one GET per lookup with a 10-second timeout, raise on a
non-2xx status (``httpx.HTTPStatusError``) and read a JSON object holding a
list of records (``items`` for Google Books, ``docs`` for Open Library).
"""

from typing import Any

import httpx

# Seconds, for connect, read, write and pool alike.
LOOKUP_TIMEOUT_S = 10


async def get_json(url: str, params: dict[str, Any]) -> Any:
    """GET ``url`` with ``params`` and return the decoded JSON body. A
    non-2xx status raises ``httpx.HTTPStatusError``; a transport error or a
    timeout propagates as ``httpx`` raises it."""
    async with httpx.AsyncClient(timeout=LOOKUP_TIMEOUT_S) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


def dict_records(data: dict, key: str) -> list[dict]:
    """The records under ``key`` (none when absent) that are objects."""
    return [r for r in data.get(key, []) if isinstance(r, dict)]


def first_record(data: dict, key: str) -> Any:
    """The first record under ``key``, or None when there is none."""
    records = data.get(key, [])
    return records[0] if records else None
