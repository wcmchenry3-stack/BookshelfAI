"""Transactional email through Resend's HTTP API (#694).

The first sender of email in the backend; later transactional mail (#678's
replies land in the support inbox) goes through :func:`send` too.

- From ``Bookshelf <EMAIL_FROM>`` (an address on the domain verified in
  Resend), ``Reply-To: EMAIL_REPLY_TO``, an HTML and a plain-text body.
- ``idempotency_key`` is passed to Resend (``Idempotency-Key``), which sends a
  repeat of the same key within 24 hours only once, so a job retried after
  a lost response does not email twice.
- A timeout (:data:`TIMEOUT_S`) and bounded retries
  (``app/services/_http_retry.py``).
- Never logs or raises with the recipient, subject, body or API key:
  :class:`EmailError` carries the HTTP status only.

``RESEND_API_KEY`` unset means email is off (:func:`configured`). Tests pass
an ``httpx.MockTransport``; nothing in the suite calls Resend.
"""

from dataclasses import dataclass

import httpx

from app.core.config import settings
from app.services._http_retry import RequestFailed, send_with_retries

API_URL = "https://api.resend.com/emails"
SENDER_NAME = "Bookshelf"
TIMEOUT_S = 15.0


class EmailError(Exception):
    """Sending failed; ``status`` is the HTTP status, or ``None`` for a
    transport error or timeout."""

    def __init__(self, status: int | None) -> None:
        super().__init__(f"email send failed (status={status})")
        self.status = status


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    html: str
    text: str


def configured() -> bool:
    return bool(settings.resend_api_key)


async def send(
    message: Message,
    *,
    idempotency_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    """Send ``message``; raise :class:`EmailError` when Resend refuses it or
    cannot be reached after the retries."""
    payload = {
        "from": f"{SENDER_NAME} <{settings.email_from}>",
        "to": [message.to],
        "reply_to": settings.email_reply_to,
        "subject": message.subject,
        "html": message.html,
        "text": message.text,
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Idempotency-Key": idempotency_key,
    }
    async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=transport) as client:
        try:
            await send_with_retries(
                client, "email send", "POST", API_URL, json=payload, headers=headers
            )
        except RequestFailed as exc:
            raise EmailError(exc.status) from None
