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
number, or a title or author over 500 characters, #754), or an unexpected
``finish_reason`` (anything but ``stop``, ``length`` or ``content_filter``; a
missing one is judged on the content alone). Otherwise the reply is ``ok``
with its well-formed items, kept as before #572: an empty author is still a
candidate (a title-only lookup). Since #746 a candidate with an empty or
whitespace title is dropped (it is schema-valid, so it is not "broken" and a
reply of only such items is ``ok`` with no books), and an ISBN that is not a
string or fails its check digit becomes None while the book is kept (the
model can invent ISBNs). Since #754 a confidence is clamped to 0-1 (NaN
becomes 0.5; an integer too large for a float clamps to 1 or 0), as in the
Claude adapter. Unlike Claude, which gives 0.5 for any string, a numeric
string ("1.5", " 0.9 ") parses and clamps here, and null, a list, an object or
a non-numeric string is broken (kept from #572). A genuine
``{"books": []}`` is ``ok`` with no candidates. A ``length`` reply stays
``truncated`` here; with no books the gateway reports it as ``malformed``.
"""

import base64
import hashlib
import json
import logging

import httpx

from app.ai.providers._common import (
    clamp_confidence,
    clean_isbn_10,
    clean_isbn_13,
    collect_candidates,
    has_broken_text,
    token_count,
)
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
    prompt = token_count(usage.get("prompt_tokens"))
    output = token_count(usage.get("completion_tokens"))
    details = usage.get("prompt_tokens_details")
    cached = (
        token_count(details.get("cached_tokens")) if isinstance(details, dict) else 0
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


def _is_broken(item: object) -> bool:
    """An item that breaks the requested structure (#572): not an object, a
    title or author missing or not a string (a ``null`` author used to crash
    the de-duplication), or a confidence that is not a number. An empty title
    or author is not broken: the item is schema-valid."""
    # Same title/author rule as the Claude adapter (#754): over-long text is
    # broken.
    if has_broken_text(item):
        return True
    try:
        float(item.get("confidence", 0.5))
    except OverflowError:
        pass  # an integer too large for a float: clamped, not broken
    except (TypeError, ValueError):
        return True
    return False


def _confidence(value: object) -> float:
    """The confidence clamped to 0-1, as the Claude adapter does (#754): NaN
    and a boolean become 0.5, infinity clamps to 0 or 1. (A non-number such as
    ``"high"`` is broken here, see :func:`_is_broken`, a rule kept from #572;
    the Claude adapter defaults it to 0.5. The difference is intentional
    until #795 decides.)"""
    if isinstance(value, int):
        # A bool or an int (exact compare: float() overflows past ~1e308).
        return clamp_confidence(value)
    number = float(value)  # type: ignore[arg-type]  # checked by _is_broken
    return clamp_confidence(number)


def _candidate(item: object) -> BookCandidate | None:
    """The candidate for one item, or None when the item is broken (see
    :func:`_is_broken`) or has an empty or whitespace title (#746: dropped,
    not broken). An empty author is kept (a title-only lookup). An ISBN that
    is not a valid string becomes None; the book is kept."""
    if _is_broken(item) or not isinstance(item, dict):
        return None
    title = item["title"]
    if not title.strip():
        return None
    return BookCandidate(
        title=title,
        author=item["author"],
        confidence=_confidence(item.get("confidence", 0.5)),
        isbn_13=clean_isbn_13(item.get("isbn_13")),
        isbn_10=clean_isbn_10(item.get("isbn_10")),
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

    def _parse_reply(
        self, content: str | None, finish_reason: str | None, max_books: int
    ) -> list[BookCandidate] | None:
        """The candidates in the model's reply, or None when the reply is
        malformed (#572): no content, not JSON, the wrong shape, or a
        non-empty list in which every item is structurally broken (see
        :func:`_is_broken`)."""
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

        # A candidate that is None is schema-valid but dropped (empty title).
        candidates, broken = collect_candidates(
            items,
            max_books,
            is_broken=_is_broken,
            to_candidate=_candidate,
            dedupe_key=_dedupe_key,
        )

        if items and broken == len(items):
            # A list none of whose items has the requested structure: not a
            # "no books" answer.
            _log_unparseable(
                "no well-formed item in a non-empty list", content, finish_reason
            )
            return None
        return candidates
