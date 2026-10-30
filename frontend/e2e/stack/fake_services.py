"""Fake external services for the web e2e suite (#470).

One stdlib HTTP server that stands in for every outbound call the backend
makes during the e2e run, so the suite never reaches a paid model or a
flaky public catalog:

- ``POST /v1/messages``: the Anthropic Messages API (``AI_ANTHROPIC_BASE_URL``).
  It is ``backend/perf/fake_provider.py`` (#539) reused as is: a fixed reply
  naming two books (Dune and The Great Gatsby).
- ``GET /search.json``: Open Library (``OPEN_LIBRARY_BASE_URL``), answered from
  the small catalog below.
- ``POST /__forget_scans`` (only when ``run_backend.py`` supplies it): delete the
  saved scan records, so a photo used by an earlier run reaches the provider again.
- ``GET /books/v1/volumes``: Google Books. The URL is a constant in the app,
  so ``run_backend.py`` points it here (see there).

Run: ``python e2e/stack/fake_services.py --port 18080`` (it puts ``backend/``
on ``sys.path`` itself).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from perf import fake_provider

# (key, title, author, isbn13, isbn10). Dune and Gatsby are the books the fake
# model "reads" from every scanned photo; Emma and Frankenstein only exist so
# the search-and-add scenario has something to find that is not in the library.
CATALOG = [
    ("OL1W", "Dune", "Frank Herbert", "9780441013593", "0441013597"),
    ("OL2W", "The Great Gatsby", "F. Scott Fitzgerald", "9780743273565", "0743273567"),
    ("OL3W", "Emma", "Jane Austen", "9780141439587", "0141439580"),
    ("OL4W", "Frankenstein", "Mary Shelley", "9780141439471", "0141439475"),
]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _match(query: str) -> list[tuple]:
    """Catalog rows for a Google Books / Open Library style query: an
    ``isbn:`` lookup, or any query word that is in the title or author."""
    isbn = re.search(r"isbn:(\d+)", query)
    if isbn:
        return [row for row in CATALOG if isbn.group(1) in (row[3], row[4])]
    wanted = _words(re.sub(r"\b(intitle|inauthor):", " ", query))
    return [row for row in CATALOG if wanted & _words(f"{row[1]} {row[2]}")]


def google_volumes(query: str, limit: int) -> dict:
    items = [
        {
            "id": f"gb{row[0].lower()}",
            "volumeInfo": {
                "title": row[1],
                "authors": [row[2]],
                "description": f"A fake description of {row[1]}.",
                "pageCount": 300,
                "publisher": "Fake House",
                "publishedDate": "1999-01-01",
                "industryIdentifiers": [
                    {"type": "ISBN_13", "identifier": row[3]},
                    {"type": "ISBN_10", "identifier": row[4]},
                ],
            },
        }
        for row in _match(query)[:limit]
    ]
    return {"totalItems": len(items), "items": items}


def open_library_docs(params: dict[str, list[str]], limit: int) -> dict:
    if "isbn" in params:
        rows = _match(f"isbn:{params['isbn'][0]}")
    else:
        rows = _match(" ".join(params.get("title", []) + params.get("author", [])))
    docs = [
        {
            "key": f"/works/{row[0]}",
            "title": row[1],
            "author_name": [row[2]],
            "isbn": [row[3], row[4]],
            "subject": ["Fiction"],
        }
        for row in rows[:limit]
    ]
    return {"numFound": len(docs), "docs": docs}


def make_handler(state, post_hooks=None):
    base = fake_provider.make_handler(state)
    post_hooks = post_hooks or {}

    class Handler(base):
        def do_GET(self):
            parts = urlsplit(self.path)
            params = parse_qs(parts.query)
            limit = int((params.get("maxResults") or params.get("limit") or ["5"])[0])
            if parts.path == "/books/v1/volumes":
                self._send(200, google_volumes((params.get("q") or [""])[0], limit))
            elif parts.path == "/search.json":
                self._send(200, open_library_docs(params, limit))
            else:
                super().do_GET()

        def do_POST(self):
            path = urlsplit(self.path).path
            if path in post_hooks:
                self._read_body()
                post_hooks[path]()
                self._send(200, {"ok": True})
            else:
                super().do_POST()

    return Handler


def make_server(
    port: int, host: str = "127.0.0.1", post_hooks=None
) -> ThreadingHTTPServer:
    """``post_hooks`` maps a POST path to a callable the e2e launcher supplies
    (``/__forget_scans``); the other paths are the fake provider's own."""
    state = fake_provider.State()
    server = ThreadingHTTPServer((host, port), make_handler(state, post_hooks))
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()
    server = make_server(args.port, args.host)
    print(json.dumps({"fake_services": f"http://{args.host}:{args.port}"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
