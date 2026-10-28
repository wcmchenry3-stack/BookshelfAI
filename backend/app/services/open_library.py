"""Open Library API — work identity, subjects, author metadata."""

import logging
from collections.abc import Callable

import httpx

from app.core.config import settings
from app.services.book_identifier import MAX_SEARCH_RESULTS, best_record

logger = logging.getLogger(__name__)


class OpenLibraryService:
    def __init__(self) -> None:
        self.base = settings.open_library_base_url

    async def search(
        self,
        title: str,
        author: str,
        *,
        limit: int = 1,
        score: Callable[[dict], int] | None = None,
    ) -> dict | None:
        """Search by title + author (author omitted when empty), one request
        for up to ``limit`` docs (capped at ``MAX_SEARCH_RESULTS``).

        Without ``score``, the first doc. With it, the doc with the highest
        positive score (ties: the provider's order), or None when every doc
        scores 0."""
        params = {
            "title": title,
            "limit": max(1, min(limit, MAX_SEARCH_RESULTS)),
            "fields": "key,title,subtitle,author_name,isbn,subject",
        }
        if author:
            params["author"] = author
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{self.base}/search.json", params=params)
            resp.raise_for_status()
            data = resp.json()

        docs = [d for d in data.get("docs", []) if isinstance(d, dict)]
        return best_record(docs, score)

    async def search_by_isbn(self, isbn: str) -> dict | None:
        """Look up a work by ISBN. Returns work dict or None."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base}/search.json",
                params={
                    "isbn": isbn,
                    "limit": 1,
                    "fields": "key,title,subtitle,author_name,isbn,subject",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        docs = data.get("docs", [])
        return docs[0] if docs else None

    def extract_work_id(self, doc: dict) -> str | None:
        """Extract bare work ID like OL45804W from /works/OL45804W key."""
        key = doc.get("key", "")
        return key.split("/")[-1] if key else None
