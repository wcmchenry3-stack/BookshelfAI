"""Structured JSON logging and per-request ID propagation."""

import json
import logging
import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
# Segments of 10+ chars so dotted names/versions (a.b.c, 1.2.3) are left alone.
_JWT_RE = re.compile(r"[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*")
# Credentials passed in URL query strings (e.g. third-party API keys that end
# up in HTTP client exception messages).
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|token|access_token|secret)=)[^&\s'\"]+"
)

# Loggers that uvicorn configures with their own handlers (propagate=False).
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def redact(text: str) -> str:
    """Replace emails, bearer/JWT-looking strings and URL credentials in ``text``."""
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    return _EMAIL_RE.sub("[REDACTED_EMAIL]", text)


class RedactingFilter(logging.Filter):
    """Safety net: scrub emails and tokens from the rendered message and traceback.

    The message is rendered once (so nested args and objects' ``__str__`` are
    covered) and stored back with no args. Exception text is rendered into
    ``exc_text``, which standard formatters use instead of re-formatting.
    Never raises; on any failure the record is passed through unchanged.
    """

    _exc_formatter = logging.Formatter()

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
            if record.exc_info and not record.exc_text:
                record.exc_text = self._exc_formatter.formatException(record.exc_info)
            if record.exc_text:
                record.exc_text = redact(record.exc_text)
            if record.stack_info:
                record.stack_info = redact(record.stack_info)
        except Exception:  # noqa: BLE001, S110 - a log filter must never raise
            pass
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": redact(record.getMessage()),
        }
        if record.exc_info or record.exc_text:
            exc_text = record.exc_text or self.formatException(record.exc_info)
            log_obj["exc_info"] = redact(exc_text)
        return json.dumps(log_obj)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [handler]
    # uvicorn's loggers don't propagate to root, so filter their handlers too.
    for name in _UVICORN_LOGGERS:
        for uv_handler in logging.getLogger(name).handlers:
            uv_handler.addFilter(RedactingFilter())


def new_request_id() -> str:
    return str(uuid.uuid4())
