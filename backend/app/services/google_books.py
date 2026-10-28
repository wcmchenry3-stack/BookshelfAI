"""Google Books API — covers, descriptions, page counts."""

import logging
from collections.abc import Callable

import httpx

from app.core.config import settings
from app.services.book_identifier import MAX_SEARCH_RESULTS, best_record

logger = logging.getLogger(__name__)

GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes"


def _phrase(value: str) -> str:
    """``value`` as a quoted query phrase (inner quotes dropped)."""
    return '"' + " ".join(value.replace('"', " ").split()) + '"'


def search_query(title: str, author: str) -> str:
    """The ``q`` of a title + author search: ``intitle:"…" inauthor:"…"``,
    the author part only when there is one."""
    query = f"intitle:{_phrase(title)}"
    if author and author.strip():
        query += f" inauthor:{_phrase(author)}"
    return query


class GoogleBooksService:
    async def search(
        self,
        title: str,
        author: str,
        *,
        limit: int = 1,
        score: Callable[[dict], int] | None = None,
    ) -> dict | None:
        """Search by title + author, one request for up to ``limit`` volumes
        (capped at ``MAX_SEARCH_RESULTS``). Each is a quoted phrase
        (``intitle:"The Impending Crisis" inauthor:"Potter"``), so every word
        of a multi-word title is matched in the title, not anywhere; the
        author is left out when empty.

        Without ``score``, the first volume. With it, the volume with the
        highest positive score (ties: the provider's order), or None when
        every volume scores 0."""
        params = {
            "q": search_query(title, author),
            "maxResults": max(1, min(limit, MAX_SEARCH_RESULTS)),
        }
        if settings.google_books_api_key:
            params["key"] = settings.google_books_api_key

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(GOOGLE_BOOKS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        items = [v for v in data.get("items", []) if isinstance(v, dict)]
        return best_record(items, score)

    async def search_query(self, query: str, limit: int = 3) -> list[dict]:
        """Free-text search. Returns up to `limit` volume dicts."""
        params = {"q": query, "maxResults": limit}
        if settings.google_books_api_key:
            params["key"] = settings.google_books_api_key

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(GOOGLE_BOOKS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        return data.get("items", [])[:limit]

    async def search_by_isbn(self, isbn: str) -> dict | None:
        """Look up by ISBN. Returns volume dict or None."""
        params = {"q": f"isbn:{isbn}", "maxResults": 1}
        if settings.google_books_api_key:
            params["key"] = settings.google_books_api_key

        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(GOOGLE_BOOKS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        items = data.get("items", [])
        return items[0] if items else None

    def extract_cover_url(self, volume: dict) -> str | None:
        """Return the largest available thumbnail URL with https."""
        image_links = volume.get("volumeInfo", {}).get("imageLinks", {})
        url = (
            image_links.get("large")
            or image_links.get("medium")
            or image_links.get("thumbnail")
        )
        return url.replace("http://", "https://") if url else None

    def extract_info(self, volume: dict) -> dict:
        """Pull the fields we care about from a volume dict."""
        info = volume.get("volumeInfo", {})
        return {
            "google_books_id": volume.get("id"),
            "description": info.get("description"),
            "cover_url": self.extract_cover_url(volume),
            "page_count": info.get("pageCount"),
            "publisher": info.get("publisher"),
            "publish_year": (info.get("publishedDate") or "")[:4] or None,
        }
