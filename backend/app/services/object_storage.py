"""A small client for Supabase Storage's REST API (#694): upload, signed
download URL, list and delete objects in one private bucket.

Generic on purpose: the account export (#694) uses it and the debug-photo
upload (#874) will. Nothing here knows what the objects are.

- Every call authenticates with the project's service-role key
  (``SUPABASE_SERVICE_ROLE_KEY``), sent only to ``SUPABASE_URL`` and never
  logged. Errors carry the HTTP status and the operation, never the URL,
  the key, a signed token or the response body (:class:`StorageError`).
- Each request has a timeout (:data:`TIMEOUT_S`) and bounded retries
  (``app/services/_http_retry.py``: 3 tries; a transport error, a timeout,
  a 429 or a 5xx is retried after a short backoff, any other status is
  final).
- Uploads overwrite (``x-upsert``), so retrying a job that already uploaded
  writes the same object again rather than failing.

``from_settings()`` builds the client from ``Settings``; it returns ``None``
while storage is not configured, which is how callers implement their kill
switch. Tests pass an ``httpx.MockTransport``; nothing in the test suite
talks to the real service.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.services._http_retry import RequestFailed, send_with_retries

# Seconds, for connect, read, write and pool alike.
TIMEOUT_S = 15.0


class StorageError(Exception):
    """A storage call failed. ``status`` is the HTTP status, or ``None`` for
    a transport error or timeout. The message names the operation only."""

    def __init__(self, operation: str, status: int | None) -> None:
        super().__init__(f"storage {operation} failed (status={status})")
        self.operation = operation
        self.status = status


@dataclass(frozen=True)
class StoredObject:
    """One object from :meth:`ObjectStorage.list`."""

    name: str
    created_at: datetime | None


def _quoted(path: str) -> str:
    return quote(path.strip("/"), safe="/")


def _created_at(entry: dict) -> datetime | None:
    raw = entry.get("created_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


class ObjectStorage:
    """Objects in one bucket of one Supabase project."""

    def __init__(
        self,
        base_url: str,
        service_key: str,
        bucket: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api = f"{base_url.rstrip('/')}/storage/v1"
        self._key = service_key
        self.bucket = bucket
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT_S,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._key}", "apikey": self._key},
        )

    async def _request(
        self, operation: str, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        """Send one request with bounded retries; return a 2xx response or
        raise :class:`StorageError`."""
        async with self._client() as client:
            try:
                return await send_with_retries(
                    client,
                    f"object_storage {operation}",
                    method,
                    f"{self._api}{path}",
                    **kwargs,
                )
            except RequestFailed as exc:
                raise StorageError(operation, exc.status) from None

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        """Write ``data`` at ``path``, replacing any object already there."""
        await self._request(
            "upload",
            "POST",
            f"/object/{self.bucket}/{_quoted(path)}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )

    async def signed_url(
        self, path: str, expires_in: int, *, download_name: str | None = None
    ) -> str:
        """A URL that downloads ``path`` without credentials until
        ``expires_in`` seconds from now. With ``download_name`` the browser
        saves it under that name. Never log the result: it is a credential."""
        resp = await self._request(
            "sign",
            "POST",
            f"/object/sign/{self.bucket}/{_quoted(path)}",
            json={"expiresIn": expires_in},
        )
        try:
            signed = resp.json()["signedURL"]
        except (ValueError, KeyError, TypeError):
            raise StorageError("sign", resp.status_code) from None
        if not isinstance(signed, str) or not signed.startswith("/"):
            raise StorageError("sign", resp.status_code)
        url = f"{self._api}{signed}"
        if download_name:
            separator = "&" if "?" in url else "?"
            url += f"{separator}download={quote(download_name, safe='')}"
        return url

    async def delete(self, paths: list[str]) -> None:
        """Delete the objects at ``paths`` (at most 1,000). Missing objects
        are not an error, so a repeated delete is harmless."""
        if not paths:
            return
        await self._request(
            "delete",
            "DELETE",
            f"/object/{self.bucket}",
            json={"prefixes": [p.strip("/") for p in paths]},
        )

    async def list(self, prefix: str, *, limit: int = 100) -> list[StoredObject]:
        """Up to ``limit`` objects directly under the folder ``prefix``,
        oldest first. Sub-folders are skipped."""
        resp = await self._request(
            "list",
            "POST",
            f"/object/list/{self.bucket}",
            json={
                "prefix": prefix.strip("/"),
                "limit": limit,
                "offset": 0,
                "sortBy": {"column": "created_at", "order": "asc"},
            },
        )
        try:
            entries = resp.json()
        except ValueError:
            raise StorageError("list", resp.status_code) from None
        if not isinstance(entries, list):
            raise StorageError("list", resp.status_code)
        return [
            StoredObject(name=entry["name"], created_at=_created_at(entry))
            for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
            # Folders come back with no id.
            and entry.get("id") is not None
        ]


def from_settings(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> ObjectStorage | None:
    """The export bucket's client, or ``None`` while ``SUPABASE_URL`` or
    ``SUPABASE_SERVICE_ROLE_KEY`` is unset."""
    if not (settings.supabase_url and settings.supabase_service_role_key):
        return None
    return ObjectStorage(
        settings.supabase_url,
        settings.supabase_service_role_key,
        settings.export_bucket,
        transport=transport,
    )
