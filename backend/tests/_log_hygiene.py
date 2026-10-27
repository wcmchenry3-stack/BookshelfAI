"""Log-hygiene helpers (#692): capture every Python log record and Sentry
event/transaction produced inside a ``with`` block, and a pure checker that
reports emails, allowlist values, tokens, subs and Authorization headers.

Capture happens at ``Logger.handle``, before any handler or filter runs, so
the app's own redaction safety net (``RedactingFilter``) cannot hide a leak:
the point is that the code never emits the value in the first place.

Later stories that add flows (account deletion, ...) should drive them inside
``capture()`` in ``tests/security/test_log_hygiene_flows.py`` and call
:func:`assert_clean`.
"""

import contextlib
import json
import logging
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field

import sentry_sdk
from sentry_sdk.transport import Transport

from app.core.sentry_context import sentry_privacy_options

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
# A compact JWT: base64url('{"') header, payload, optional signature.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*")
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/\-]{8,}=*")
# Header-ish "authorization: <value>" in text, or "authorization": "<value>" in JSON.
_AUTH_HEADER_RE = re.compile(
    r"(?i)authorization[\"']?\s*[:=]\s*[\"']?(?!\[?(?:filtered|redacted)\b)[^\s\"',}]{4,}"
)


@dataclass(frozen=True)
class Finding:
    kind: str  # email | allowlist | jwt | secret | authorization
    source: str  # "log" or "sentry"
    sample: str = field(repr=False, default="")

    def __str__(self) -> str:  # never print the leaked value itself
        return f"{self.kind} found in {self.source}"


class Captured:
    """Everything emitted while :func:`capture` was active."""

    def __init__(self) -> None:
        self.log_texts: list[str] = []
        self.error_texts: list[str] = []  # ERROR and above
        self.sentry_payloads: list[dict] = []

    @property
    def sentry_texts(self) -> list[str]:
        return [json.dumps(p, default=str) for p in self.sentry_payloads]


def check_texts(
    texts: Iterable[str],
    *,
    source: str,
    emails: Iterable[str] = (),
    allowlist: Iterable[str] = (),
    secrets: Iterable[str] = (),
) -> list[Finding]:
    """Pure check of ``texts``. ``emails`` are the test users' addresses,
    ``allowlist`` the configured ALLOWED_EMAILS entries, ``secrets`` exact
    token / Google-or-Apple ``sub`` / ID-token values used in the test."""
    exact = [("email", e) for e in emails if e]
    exact += [("allowlist", a) for a in allowlist if a]
    exact += [("secret", s) for s in secrets if s]
    # Also the URL-encoded form (a value inside a logged URL or query string).
    for kind, value in list(exact):
        for encoded in (urllib.parse.quote(value, safe=""), urllib.parse.quote(value)):
            if encoded != value:
                exact.append((kind, encoded))
    findings: list[Finding] = []
    for text in texts:
        for kind, value in exact:
            if value.lower() in text.lower():
                findings.append(Finding(kind, source, value))
        for kind, rx in (
            ("email", _EMAIL_RE),
            ("jwt", _JWT_RE),
            ("authorization", _BEARER_RE),
            ("authorization", _AUTH_HEADER_RE),
        ):
            m = rx.search(text)
            if m:
                findings.append(Finding(kind, source, m.group(0)))
    return findings


def check(captured: Captured, **kwargs) -> list[Finding]:
    return check_texts(captured.log_texts, source="log", **kwargs) + check_texts(
        captured.sentry_texts, source="sentry", **kwargs
    )


def assert_clean(captured: Captured, **kwargs) -> None:
    findings = check(captured, **kwargs)
    assert not findings, "log hygiene violations: " + ", ".join(
        sorted({str(f) for f in findings})
    )


_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage()]
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    if record.exc_text:
        parts.append(record.exc_text)
    if record.stack_info:
        parts.append(record.stack_info)
    for key, value in record.__dict__.items():
        if key not in _STD_ATTRS:
            parts.append(f"{key}={value!r}")
    return "\n".join(parts)


@contextlib.contextmanager
def capture():
    """Collect log records (pre-redaction) and Sentry events + transactions."""
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration

    captured = Captured()
    original_handle = logging.Logger.handle

    def handle(self, record):
        try:
            text = _record_text(record)
            captured.log_texts.append(text)
            if record.levelno >= logging.ERROR:
                captured.error_texts.append(text)
        except Exception:  # noqa: BLE001, S110 - capture must never break the flow
            pass
        return original_handle(self, record)

    class _Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") in ("event", "transaction"):
                    captured.sentry_payloads.append(item.payload.json)

    scope = sentry_sdk.get_global_scope()
    previous_client = scope.client
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.DEBUG)
    logging.Logger.handle = handle  # type: ignore[method-assign]
    try:
        with sentry_sdk.isolation_scope(), sentry_sdk.new_scope():
            client = sentry_sdk.Client(
                dsn="https://abc@o1.ingest.us.sentry.io/1",
                transport=_Capture,
                integrations=[
                    StarletteIntegration(transaction_style="endpoint"),
                    FastApiIntegration(transaction_style="endpoint"),
                ],
                traces_sample_rate=1.0,
                send_default_pii=False,
                **sentry_privacy_options(),
            )
            sentry_sdk.get_global_scope().set_client(client)
            try:
                yield captured
            finally:
                client.flush()
                client.close()
    finally:
        scope.set_client(previous_client)
        logging.Logger.handle = original_handle  # type: ignore[method-assign]
        root.setLevel(old_level)
