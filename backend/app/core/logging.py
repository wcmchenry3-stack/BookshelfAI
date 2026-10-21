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


def _redact_value(value):
    return redact(value) if isinstance(value, str) else value


class RedactingFilter(logging.Filter):
    """Safety net: scrub emails and tokens from the message and traceback.

    Default mode renders the message once (so nested args and objects'
    ``__str__`` are covered), redacts it and clears the args. That suits
    formatters that only use ``getMessage()``, like :class:`JsonFormatter`.

    ``preserve_args=True`` redacts string args in place and keeps their
    number and order, for formatters that unpack ``record.args`` themselves
    (uvicorn's access formatter expects a 5-tuple).

    Exception text is rendered into ``exc_text`` and redacted in both modes.
    Never raises; on any failure the record is passed through unchanged.
    """

    _exc_formatter = logging.Formatter()

    def __init__(self, name: str = "", *, preserve_args: bool = False) -> None:
        super().__init__(name)
        self.preserve_args = preserve_args

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if self.preserve_args:
                # Only redact a format string with no placeholders: redacting
                # around a "%s" could change the placeholder count.
                if isinstance(record.msg, str) and "%" not in record.msg:
                    record.msg = redact(record.msg)
                if isinstance(record.args, tuple):
                    record.args = tuple(_redact_value(a) for a in record.args)
                elif isinstance(record.args, dict):
                    record.args = {k: _redact_value(v) for k, v in record.args.items()}
            else:
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
    # Their formatters unpack record.args, so keep the args shape. This relies
    # on uvicorn having configured its handlers before the app is imported,
    # which is the case for `uvicorn app.main:app` (render.yaml, Dockerfile).
    for name in _UVICORN_LOGGERS:
        for uv_handler in logging.getLogger(name).handlers:
            uv_handler.addFilter(RedactingFilter(preserve_args=True))


def new_request_id() -> str:
    return str(uuid.uuid4())
