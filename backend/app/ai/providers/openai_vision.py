"""OpenAI vision provider — finds every book in a photo.

Moved with no behaviour change from ``app/services/chatgpt_vision.py``
(``ChatGPTVisionIdentifier``). The model, token cap and timeout now come from
the feature's ``FeatureSpec`` instead of being picked here from ``settings``;
the values are the same.

Since #530 ``identify`` returns a ``ProviderResult``: the candidates plus the
billed usage and the outcome (``ok``, ``refused``, ``truncated`` or
``malformed``) for the usage ledger.

``malformed`` (#572): a 200 whose reply is not a usable answer: no message
content (and no refusal), content that is not JSON, JSON that is not a
``{"books": [...]}`` object (a bare list is still accepted, as before), a
non-empty list in which every item is structurally broken (not an object, a
title or author missing or not a string, or a confidence that is not a
number), or an unexpected ``finish_reason`` (anything but ``stop``,
``length`` or ``content_filter``; a missing one is judged on the content
alone). Otherwise the reply is ``ok`` with its well-formed items, kept as
before #572: an empty author (or title) is still a candidate (a title-only
lookup). A genuine ``{"books": []}`` is ``ok`` with no candidates. A
``length`` reply stays ``truncated`` here; with no books the gateway reports
it as ``malformed``.
"""

import base64
import hashlib
import json
import logging

import httpx

from app.ai.providers.base import (
    ProviderOutcome,
    ProviderResult,
    ProviderTimeoutError,
)
from app.ai.registry import FeatureSpec
from app.core.config import settings
from app.services.book_identifier import BookCandidate, ScanUnavailableError

logger = logging.getLogger(__name__)

OPENAI_URL = "https://api.openai.com/v1/chat/completions"

SYSTEM_PROMPT = """You are a book identification assistant.
You will be shown a photo that may contain one book or many: a single cover, a stack of covers, or a shelf of book spines.
Identify EVERY distinct book you can read in the photo, up to {max_books} books.

Rules:
- Each physical book appears at most once. Do not list the same book twice.
- Read spine text carefully; spines are often rotated 90 degrees.
- Skip a book entirely if you cannot read enough of its title to identify it. Never invent books.
- Order books as they appear (left to right, then top to bottom).

For each book return:
- title: string
- author: string
- confidence: float between 0 and 1 (how sure you are of this identification)
- isbn_13: string or null (only if a 13-digit ISBN is printed and legible in the photo)
- isbn_10: string or null (only if a 10-digit ISBN is printed and legible in the photo)

Give an ISBN only when you can read it on that book in the photo; otherwise use null (most photos show no ISBN). Never guess or recall an ISBN from memory: the book is looked up by its title and author.
Return ONLY a JSON object of the form {{"books": [...]}}. Return {{"books": []}} if no book is readable. Example:
{{"books":[{{"title":"Dune","author":"Frank Herbert","confidence":0.97,"isbn_13":null,"isbn_10":null}}]}}"""


def _message_content(body: object) -> tuple[str | None, str | None]:
    """Return ``(content, finish_reason)`` from a chat-completions body.

    ``content`` is None when the body has an unexpected shape or the message
    carries no text (e.g. the model declined and returned a refusal instead).
    """
    try:
        choice = body["choices"][0]  # type: ignore[index]
        finish_reason = choice.get("finish_reason")
        content = choice["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None, None
    if not isinstance(content, str):
        return None, finish_reason if isinstance(finish_reason, str) else None
    return content, finish_reason


# The ledger's token columns are Postgres INTEGER.
MAX_TOKEN_COUNT = 2_147_483_647


def _token_count(value: object) -> int:
    """A token count from the usage block: an int clamped to
    ``[0, MAX_TOKEN_COUNT]``; anything else is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_TOKEN_COUNT)


def _usage(body: object) -> tuple[int, int, int]:
    """Return ``(input, output, cache_read)`` billed tokens from ``usage``.

    OpenAI's ``prompt_tokens`` includes the cached prompt tokens
    (``prompt_tokens_details.cached_tokens``), which are billed at the cached
    input price. The ledger's counts are disjoint, so ``input`` is the
    uncached part only. A missing or garbled usage block gives zeros.
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    prompt = _token_count(usage.get("prompt_tokens"))
    output = _token_count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = (
        _token_count(details.get("cached_tokens")) if isinstance(details, dict) else 0
    )
    cached = min(cached, prompt)
    return prompt - cached, output, cached


def _has_refusal(body: object) -> bool:
    try:
        refusal = body["choices"][0]["message"].get("refusal")  # type: ignore[index]
    except (KeyError, IndexError, TypeError, AttributeError):
        return False
    return isinstance(refusal, str) and bool(refusal)


# The finish reason of a finished answer (None: not reported; the content
# decides).
NORMAL_FINISH_REASONS = frozenset({"stop", None})


def _outcome(body: object, finish_reason: str | None) -> ProviderOutcome:
    """``refused`` only when the model declined (a ``refusal`` field or a
    ``content_filter`` stop), ``truncated`` when it hit ``max_tokens``,
    ``malformed`` for any other unexpected finish reason (#572), else ``ok``
    (the content may still make it ``malformed``, see ``identify``)."""
    if finish_reason == "content_filter" or _has_refusal(body):
        return "refused"
    if finish_reason == "length":
        return "truncated"
    if finish_reason not in NORMAL_FINISH_REASONS:
        return "malformed"
    return "ok"


def _log_unparseable(kind: str, content: str, finish_reason: str | None) -> None:
    """Log a model-output parse failure without logging the content itself."""
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:12]
    logger.warning(
        "GPT returned %s: length=%d sha256=%s finish_reason=%s",
        kind,
        len(content),
        digest,
        finish_reason or "unknown",
    )


def _candidate(item: object) -> BookCandidate | None:
    """The candidate for one item, or None when the item is structurally
    broken (#572): not an object, a title or author missing or not a string
    (a ``null`` author used to crash the de-duplication), or a confidence
    that is not a number. Strings are kept as they are, empty ones too."""
    if not isinstance(item, dict):
        return None
    title, author = item.get("title"), item.get("author")
    if not isinstance(title, str) or not isinstance(author, str):
        return None
    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        return None
    return BookCandidate(
        title=title,
        author=author,
        confidence=confidence,
        isbn_13=item.get("isbn_13"),
        isbn_10=item.get("isbn_10"),
    )


def _dedupe_key(candidate: BookCandidate) -> tuple[str, str]:
    return (candidate.title.strip().casefold(), candidate.author.strip().casefold())


class OpenAIVisionProvider:
    def __init__(self, spec: FeatureSpec) -> None:
        if spec.provider != "openai":
            raise ValueError(f"{spec.name} is not an OpenAI feature")
        self.spec = spec

    async def identify(self, image_bytes: bytes) -> ProviderResult:
        b64 = base64.standard_b64encode(image_bytes).decode()
        max_books = settings.scan_max_books

        payload = {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(max_books=max_books),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            # Spine text is small; "high" detail tiles the image so
                            # the model can actually read it.
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64}",
                                "detail": "high",
                            },
                        },
                        {"type": "text", "text": "Identify every book in this photo."},
                    ],
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=self.spec.timeout_s) as client:
                resp = await client.post(
                    OPENAI_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                )
                resp.raise_for_status()
        except (httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # No connection was made, so nothing was sent or billed: the same
            # "never connected" path as httpx.ConnectError (units refunded,
            # cost estimate given back, #531), not a provider timeout.
            logger.warning("OpenAI vision request could not connect: %s", exc)
            raise httpx.ConnectError("Vision API: no connection made") from exc
        except httpx.TimeoutException as exc:
            # Read/write timeout: the request reached the provider.
            logger.warning("OpenAI vision request timed out: %s", exc)
            raise ProviderTimeoutError("Vision API timed out") from exc
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning("OpenAI rate limit exceeded (429)")
            else:
                logger.warning(
                    "OpenAI vision request failed with HTTP %s",
                    exc.response.status_code,
                )
            raise ScanUnavailableError("Vision API returned an error") from exc

        try:
            body = resp.json()
        except ValueError:
            body = None
        content, finish_reason = _message_content(body)
        input_tokens, output_tokens, cache_read = _usage(body)
        outcome = _outcome(body, finish_reason)
        candidates: list[BookCandidate] = []
        if outcome == "malformed":
            logger.warning("GPT returned an unexpected finish_reason=%s", finish_reason)
        else:
            # A refusal is parsed as before (out of scope for #572).
            parsed = self._parse_reply(content, finish_reason, max_books)
            if parsed is None and outcome == "ok":
                # Missing, non-JSON or structurally broken content (#572). A
                # truncated reply stays ``truncated`` here; with no books the
                # gateway reports it as ``malformed``.
                outcome = "malformed"
            candidates = parsed or []
        return ProviderResult(
            candidates=candidates,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,
            outcome=outcome,
        )

    def _parse(
        self, content: str | None, finish_reason: str | None, max_books: int
    ) -> list[BookCandidate]:
        """The candidates in the model's reply (empty when malformed)."""
        return self._parse_reply(content, finish_reason, max_books) or []

    def _parse_reply(
        self, content: str | None, finish_reason: str | None, max_books: int
    ) -> list[BookCandidate] | None:
        """The candidates in the model's reply, or None when the reply is
        malformed (#572): no content, not JSON, the wrong shape, or a
        non-empty list in which every item is structurally broken (see
        :func:`_candidate`)."""
        if content is None:
            # No text to parse (an unexpected body). The response itself is
            # not logged.
            logger.warning(
                "GPT response had no message content: finish_reason=%s",
                finish_reason or "unknown",
            )
            return None
        content = content.strip()

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            _log_unparseable("non-JSON", content, finish_reason)
            return None

        # JSON mode yields {"books": [...]}; tolerate a bare array too.
        items = raw.get("books") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            _log_unparseable("unexpected JSON shape", content, finish_reason)
            return None

        candidates: list[BookCandidate] = []
        seen: set[tuple[str, str]] = set()
        broken = 0
        for item in items:
            if len(candidates) >= max_books:
                break
            candidate = _candidate(item)
            if candidate is None:
                broken += 1
                continue
            key = _dedupe_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        if items and broken == len(items):
            # A list none of whose items has the requested structure: not a
            # "no books" answer.
            _log_unparseable(
                "no well-formed item in a non-empty list", content, finish_reason
            )
            return None
        return candidates
