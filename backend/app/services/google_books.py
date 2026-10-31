"""Google Books API — covers, descriptions, page counts."""

import logging
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.services._lookup_http import dict_records, first_record, get_json
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


def _params(q: str, max_results: int) -> dict[str, Any]:
    """A volumes query: ``q``, ``maxResults`` and the API key when one is
    set."""
    params: dict[str, Any] = {"q": q, "maxResults": max_results}
    if settings.google_books_api_key:
        params["key"] = settings.google_books_api_key
    return params


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
        params = _params(
            search_query(title, author), max(1, min(limit, MAX_SEARCH_RESULTS))
        )
        data = await get_json(GOOGLE_BOOKS_URL, params)
        return best_record(dict_records(data, "items"), score)

    async def search_query(self, query: str, limit: int = 3) -> list[dict]:
        """Free-text search. Returns up to `limit` volume dicts."""
        data = await get_json(GOOGLE_BOOKS_URL, _params(query, limit))
        return data.get("items", [])[:limit]

    async def search_by_isbn(self, isbn: str) -> dict | None:
        """Look up by ISBN. Returns volume dict or None."""
        data = await get_json(GOOGLE_BOOKS_URL, _params(f"isbn:{isbn}", 1))
        return first_record(data, "items")

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
