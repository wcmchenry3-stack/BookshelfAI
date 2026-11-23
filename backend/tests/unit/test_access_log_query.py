"""Search text must not reach the uvicorn access log (#1029)."""

import io
import logging

from uvicorn.logging import AccessFormatter

from app.core.logging import AccessLogQueryFilter, configure_logging


def _record(path: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5678", "GET", path, "1.1", 200),
        None,
    )


def _format(record: logging.LogRecord) -> str:
    return AccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)


def test_query_string_is_removed_but_path_and_status_kept():
    rec = _record("/books/search?q=dune&limit=5")
    assert AccessLogQueryFilter().filter(rec) is True
    out = _format(rec)
    assert "dune" not in out
    assert "GET /books/search HTTP/1.1" in out
    assert "200" in out


def test_fragment_and_plain_paths_are_handled():
    rec = _record("/books/search#frag")
    AccessLogQueryFilter().filter(rec)
    assert "/books/search HTTP" in _format(rec)
    plain = _record("/health")
    AccessLogQueryFilter().filter(plain)
    assert "/health HTTP" in _format(plain)


def test_unexpected_args_shape_passes_through():
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, "x %s", ("a",), None
    )
    assert AccessLogQueryFilter().filter(rec) is True
    assert rec.args == ("a",)


def test_configure_logging_covers_the_real_access_logger_end_to_end():
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    access = logging.getLogger("uvicorn.access")
    saved_filters = list(access.filters)
    saved_access_handlers, saved_propagate = list(access.handlers), access.propagate
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        AccessFormatter(fmt='"%(request_line)s" %(status_code)s', use_colors=False)
    )
    access.handlers = [handler]
    access.propagate = False
    access.setLevel(logging.INFO)
    try:
        configure_logging()
        configure_logging()  # idempotent: one filter only
        assert sum(isinstance(f, AccessLogQueryFilter) for f in access.filters) == 1
        access.info(
            '%s - "%s %s HTTP/%s" %d',
            "1.2.3.4:5678",
            "GET",
            "/books/search?q=dune",
            "1.1",
            200,
        )
        out = stream.getvalue()
        assert "dune" not in out
        assert "/books/search" in out and "200" in out
    finally:
        access.filters = saved_filters
        access.handlers = saved_access_handlers
        access.propagate = saved_propagate
        root.handlers = saved_handlers
        root.setLevel(saved_level)
