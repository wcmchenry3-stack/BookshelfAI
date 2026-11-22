"""Open Library API — work identity, subjects, author metadata."""

import logging
from collections.abc import Callable

from app.core.config import settings
from app.services._lookup_http import dict_records, first_record, get_json
from app.services.book_identifier import MAX_SEARCH_RESULTS, best_record

logger = logging.getLogger(__name__)

# The fields every search asks for.
_SEARCH_FIELDS = "key,title,subtitle,author_name,isbn,subject"


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
            "fields": _SEARCH_FIELDS,
        }
        if author:
            params["author"] = author
        data = await get_json(f"{self.base}/search.json", params)
        return best_record(dict_records(data, "docs"), score)

    async def search_by_isbn(self, isbn: str) -> dict | None:
        """Look up a work by ISBN. Returns work dict or None."""
        data = await get_json(
            f"{self.base}/search.json",
            {"isbn": isbn, "limit": 1, "fields": _SEARCH_FIELDS},
        )
        return first_record(data, "docs")

    def extract_work_id(self, doc: dict) -> str | None:
        """Extract bare work ID like OL45804W from /works/OL45804W key."""
        key = doc.get("key", "")
        return key.split("/")[-1] if key else None
