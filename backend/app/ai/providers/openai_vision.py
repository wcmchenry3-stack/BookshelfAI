"""OpenAI vision provider — finds every book in a photo.

Moved with no behaviour change from ``app/services/chatgpt_vision.py``
(``ChatGPTVisionIdentifier``). The model, token cap and timeout now come from
the feature's ``FeatureSpec`` instead of being picked here from ``settings``;
the values are the same.

Since #530 ``identify`` returns a ``ProviderResult``: the candidates (parsed
exactly as before) plus the billed usage and the outcome (``ok``, ``refused``
or ``truncated``) for the usage ledger.
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
- isbn_13: string or null (any valid 13-digit ISBN for this book — any edition is fine)
- isbn_10: string or null (any valid 10-digit ISBN for this book — any edition is fine)

Provide at least one ISBN whenever you can identify the book, even if you are not certain which specific edition is shown. The ISBN will only be used as a search hint.
Return ONLY a JSON object of the form {{"books": [...]}}. Return {{"books": []}} if no book is readable. Example:
{{"books":[{{"title":"Dune","author":"Frank Herbert","confidence":0.97,"isbn_13":"9780441013593","isbn_10":null}}]}}"""


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


def _outcome(body: object, finish_reason: str | None) -> ProviderOutcome:
    """``refused`` only when the model declined (a ``refusal`` field or a
    ``content_filter`` stop), ``truncated`` when it hit ``max_tokens``, else
    ``ok``: a 200 with missing, malformed or shapeless content is still an
    answered (and billed) call, just with no books."""
    if finish_reason == "content_filter" or _has_refusal(body):
        return "refused"
    if finish_reason == "length":
        return "truncated"
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
        return ProviderResult(
            candidates=self._parse(content, finish_reason, max_books),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=0,
            outcome=_outcome(body, finish_reason),
        )

    def _parse(
        self, content: str | None, finish_reason: str | None, max_books: int
    ) -> list[BookCandidate]:
        """The candidates in the model's reply (empty when unparseable)."""
        if content is None:
            # No text to parse (refusal or unexpected body). Treated as an empty
            # result; the response itself is not logged.
            logger.warning(
                "GPT response had no message content: finish_reason=%s",
                finish_reason or "unknown",
            )
            return []
        content = content.strip()

        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            _log_unparseable("non-JSON", content, finish_reason)
            return []

        # JSON mode yields {"books": [...]}; tolerate a bare array too.
        items = raw.get("books", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            _log_unparseable("unexpected JSON shape", content, finish_reason)
            return []

        candidates: list[BookCandidate] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if len(candidates) >= max_books:
                break
            try:
                candidate = BookCandidate(
                    title=item["title"],
                    author=item["author"],
                    confidence=float(item.get("confidence", 0.5)),
                    isbn_13=item.get("isbn_13"),
                    isbn_10=item.get("isbn_10"),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            key = _dedupe_key(candidate)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)

        return candidates
