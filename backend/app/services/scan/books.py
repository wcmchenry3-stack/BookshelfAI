"""The scan's enriched books, after enrichment."""

from app.schemas.book import EnrichedBook


def dedupe_enriched(books: list[EnrichedBook]) -> list[EnrichedBook]:
    """Drop books that resolved to the same work as an earlier one.

    The vision model dedupes by title/author, but two differently-read spines
    (e.g. a subtitle vs. none) can still enrich to the same Open Library work.
    """
    seen: set[str] = set()
    unique: list[EnrichedBook] = []
    for book in books:
        keys = [
            f"ol:{book.open_library_work_id}" if book.open_library_work_id else None,
            f"gb:{book.google_books_id}" if book.google_books_id else None,
        ]
        keys = [k for k in keys if k]
        if any(k in seen for k in keys):
            continue
        seen.update(keys)
        unique.append(book)
    return unique
