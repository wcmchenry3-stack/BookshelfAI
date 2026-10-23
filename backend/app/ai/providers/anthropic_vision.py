"""Claude vision provider (#532) — finds every book in a photo.

Calls the Messages API through the official ``anthropic`` SDK. Everything that
shapes the call comes from the feature's ``FeatureSpec`` (model, ``max_tokens``,
effort, timeout) and server settings (``scan_max_books``); nothing from the
client reaches it.

The request:

- the system prompt is the OpenAI adapter's ``SYSTEM_PROMPT``, word for word,
  so accuracy comparisons between providers stay meaningful; it carries a
  ``cache_control`` breakpoint (5-minute TTL). The prompt is about 300
  tokens, under these models' 512-token minimum cacheable prefix, so the
  breakpoint is most likely a no-op (no error; ``cache_creation_input_tokens``
  stays 0). Confirm on staging from the ledger's cache token columns before
  counting on cache savings;
- structured output (``output_config.format``, a JSON schema for
  ``{"books": [{title, author, confidence, isbn_13, isbn_10}]}``). The schema
  states ``maxItems = scan_max_books``; the API does not enforce array-size
  constraints, so ``anthropic.transform_schema`` moves it into the schema's
  description and the parser enforces the cap. The output format cannot carry
  a ``cache_control`` marker; the API caches its compiled grammar itself;
- ``output_config.effort`` from the spec; no ``temperature`` / ``top_p`` /
  ``top_k`` and no assistant prefill (both rejected by these models);
- the image as base64 JPEG (``app.ai.images`` re-encodes every upload).

The result: ``stop_reason == "refusal"`` is an empty ``refused`` result,
``max_tokens`` is ``truncated`` with the complete books that were parsed, and
``end_turn`` is ``ok``. Every book is validated (pydantic); an ISBN with the
wrong digit count or checksum is dropped and the title and author are kept.
The output is only ever a search key for enrichment.

``malformed`` (#572), an answered and billed call with no usable answer: any
other stop reason (or none), or an ``end_turn`` reply whose text is missing,
not JSON, not a ``{"books": [...]}`` object, or a non-empty list in which
every item is structurally broken (not an object, a title or author missing,
not a string or over ``MAX_TEXT_LENGTH``). Otherwise the reply is ``ok``:
broken items are dropped, and so are schema-valid items with an empty or
whitespace title or author (as before #572), so the list may end up empty. A
genuine ``{"books": []}`` is ``ok`` with no candidates. A ``max_tokens`` reply
stays ``truncated`` here; with no books the gateway reports it as
``malformed``.

Errors: the SDK's own retries are off (``max_retries=0``); this module retries
429, 500, 503 and 529 at most ``MAX_RETRIES`` times with exponential backoff
and full jitter, honouring ``retry-after``, and never sleeps past the spec's
``timeout_s`` (the gateway's overall deadline also cancels the whole call).
After that, or on any other 4xx/5xx, it raises ``ScanUnavailableError``
(refunded). A timeout raises ``ProviderTimeoutError``; a connection that was
never established raises ``ProviderConnectError`` (both refunded). A
connection that dropped mid-request propagates as the SDK's
``APIConnectionError`` (the reservation is kept: the call may have been
billed), as an ``httpx`` transport error does in the OpenAI adapter.

Logging: only lengths, a SHA-256 prefix, stop reasons, HTTP status codes and
error types. Never the model's text, the image or the API key.
"""

import base64
import hashlib
import json
import logging
import math
import random
import re
from typing import Any

import anthropic
import anyio
import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.ai.providers.base import (
    ProviderConnectError,
    ProviderOutcome,
    ProviderResult,
    ProviderTimeoutError,
)
from app.ai.providers.openai_vision import SYSTEM_PROMPT
from app.ai.registry import FeatureSpec
from app.core.config import settings
from app.services.book_identifier import BookCandidate, ScanUnavailableError

logger = logging.getLogger(__name__)

# The Anthropic API host. Always passed to the client explicitly, so the SDK's
# own reading of ANTHROPIC_BASE_URL never applies. ``settings.ai_anthropic_base_url``
# (AI_ANTHROPIC_BASE_URL, validated at boot: only a local host in staging and
# production) replaces it only when set, which the nightly load run does to
# reach its fake provider (#539).
ANTHROPIC_BASE_URL = "https://api.anthropic.com"

USER_TEXT = "Identify every book in this photo."

MAX_RETRIES = 2
RETRYABLE_STATUSES = frozenset({429, 500, 503, 529})
BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0

# The ledger's token columns are Postgres INTEGER.
MAX_TOKEN_COUNT = 2_147_483_647
# Title/author are search keys; anything longer is not a real title.
MAX_TEXT_LENGTH = 500


# ---------------------------------------------------------------------------
# Output schema and validation
# ---------------------------------------------------------------------------


def output_schema(max_books: int) -> dict[str, Any]:
    """The JSON schema the model's reply must follow (before the SDK's
    ``transform_schema``, which moves unsupported constraints such as
    ``maxItems`` into descriptions)."""
    nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "books": {
                "type": "array",
                "maxItems": max_books,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "author": {"type": "string"},
                        "confidence": {"type": "number"},
                        "isbn_13": nullable_str,
                        "isbn_10": nullable_str,
                    },
                    "required": ["title", "author", "confidence", "isbn_13", "isbn_10"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["books"],
        "additionalProperties": False,
    }


_ISBN_SEPARATORS = re.compile(r"[\s-]")


def _isbn_13(value: str | None) -> str | None:
    """``value`` as 13 digits with a valid checksum, else None."""
    if not isinstance(value, str):
        return None
    digits = _ISBN_SEPARATORS.sub("", value)
    if len(digits) != 13 or not digits.isascii() or not digits.isdigit():
        return None
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    return digits if total % 10 == 0 else None


def _isbn_10(value: str | None) -> str | None:
    """``value`` as 10 characters (last may be ``X``) with a valid checksum,
    else None."""
    if not isinstance(value, str):
        return None
    chars = _ISBN_SEPARATORS.sub("", value).upper()
    if (
        len(chars) != 10
        or not chars.isascii()
        or not chars[:9].isdigit()
        or not (chars[9].isdigit() or chars[9] == "X")
    ):
        return None
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(chars))
    return chars if total % 11 == 0 else None


class _Book(BaseModel):
    """One book in the model's reply. Extra keys are ignored; a bad ISBN is
    dropped (None), never the book."""

    model_config = ConfigDict(extra="ignore")

    title: str
    author: str
    confidence: float = 0.5
    isbn_13: str | None = None
    isbn_10: str | None = None

    @field_validator("title", "author", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        if not isinstance(value, str):
            # pydantic turns ValueError (not TypeError) into a validation error.
            raise ValueError("must be a string")  # noqa: TRY004
        value = value.strip()
        if not value or len(value) > MAX_TEXT_LENGTH:
            raise ValueError("empty or too long")
        return value

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0.5
        if math.isnan(value):
            return 0.5
        return min(max(float(value), 0.0), 1.0)

    @field_validator("isbn_13", mode="before")
    @classmethod
    def _valid_isbn_13(cls, value: Any) -> str | None:
        return _isbn_13(value)

    @field_validator("isbn_10", mode="before")
    @classmethod
    def _valid_isbn_10(cls, value: Any) -> str | None:
        return _isbn_10(value)


def _complete_items(text: str) -> list[Any]:
    """The complete JSON values in the ``books`` array of a reply cut off by
    ``max_tokens`` (``{"books":[{...},{...},{"tit``): every value that parses
    whole, in order. Empty when the array never started."""
    match = re.search(r'"books"\s*:\s*\[', text)
    if match is None:
        return []
    decoder = json.JSONDecoder()
    items: list[Any] = []
    pos = match.end()
    while True:
        while pos < len(text) and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= len(text) or text[pos] == "]":
            return items
        try:
            item, pos = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            return items
        items.append(item)


def _dedupe_key(candidate: BookCandidate) -> tuple[str, str]:
    return (candidate.title.casefold(), candidate.author.casefold())


def _log_content(kind: str, text: str, stop_reason: str | None) -> None:
    """Log a reply without logging its content."""
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]
    logger.warning(
        "Claude returned %s: length=%d sha256=%s stop_reason=%s",
        kind,
        len(text),
        digest,
        stop_reason or "unknown",
    )


def _is_broken(item: Any) -> bool:
    """An item that breaks the requested structure (#572): not an object, a
    title or author that is missing or not a string, or one longer than
    ``MAX_TEXT_LENGTH``. An empty or whitespace title or author is not broken:
    the item is schema-valid and only dropped."""
    if not isinstance(item, dict):
        return True
    for key in ("title", "author"):
        value = item.get(key)
        if not isinstance(value, str) or len(value.strip()) > MAX_TEXT_LENGTH:
            return True
    return False


def parse_reply(
    text: str, stop_reason: str | None, max_books: int
) -> list[BookCandidate] | None:
    """The validated, de-duplicated candidates in a reply (at most
    ``max_books``), or None when the reply is malformed (#572): not JSON, not
    a ``{"books": [...]}`` object, or a non-empty list in which every item is
    structurally broken (see :func:`_is_broken`). Schema-valid items with an
    empty or whitespace title or author are dropped, as before, so such a
    reply can be ``ok`` with fewer books or none. A genuine ``{"books": []}``
    is an empty list, not None.

    A reply cut off by ``max_tokens`` keeps the complete items it holds; when
    it holds none it is an empty list (the gateway reports a truncated reply
    with no books as ``malformed``, see ``app.ai.gateway``)."""
    items: list[Any]
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        if stop_reason == "max_tokens":
            items = _complete_items(text)
            if not items:
                _log_content("no parseable JSON", text, stop_reason)
                return []
        else:
            _log_content("no parseable JSON", text, stop_reason)
            return None
    else:
        books = raw.get("books") if isinstance(raw, dict) else None
        if not isinstance(books, list):
            _log_content("an unexpected JSON shape", text, stop_reason)
            return None
        items = books

    candidates: list[BookCandidate] = []
    seen: set[tuple[str, str]] = set()
    broken = 0
    for item in items:
        if len(candidates) >= max_books:
            break
        if _is_broken(item):
            broken += 1
            continue
        try:
            book = _Book.model_validate(item)
        except ValidationError:
            # Schema-valid but empty or whitespace: dropped, not broken.
            continue
        candidate = BookCandidate(
            title=book.title,
            author=book.author,
            confidence=book.confidence,
            isbn_13=book.isbn_13,
            isbn_10=book.isbn_10,
        )
        key = _dedupe_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    if items and broken == len(items):
        # A list none of whose items has the requested structure: not a
        # "no books" answer.
        _log_content("no well-formed item in a non-empty list", text, stop_reason)
        return None
    return candidates


def parse_books(
    text: str, stop_reason: str | None, max_books: int
) -> list[BookCandidate]:
    """:func:`parse_reply`, with a malformed reply as an empty list."""
    return parse_reply(text, stop_reason, max_books) or []


# ---------------------------------------------------------------------------
# Usage, outcome
# ---------------------------------------------------------------------------


def _token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return min(max(value, 0), MAX_TOKEN_COUNT)


def _usage(message: Any) -> tuple[int, int, int, int]:
    """``(input, output, cache_read, cache_write)`` billed tokens. Claude's
    ``input_tokens`` already excludes cached tokens, so the counts are
    disjoint as the ledger expects. Missing fields give zeros."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return 0, 0, 0, 0
    return (
        _token_count(getattr(usage, "input_tokens", 0)),
        _token_count(getattr(usage, "output_tokens", 0)),
        _token_count(getattr(usage, "cache_read_input_tokens", 0)),
        _token_count(getattr(usage, "cache_creation_input_tokens", 0)),
    )


# The stop reason of a finished, well-formed answer. Anything else that is not
# a refusal or ``max_tokens`` (``tool_use``, ``pause_turn``, ``stop_sequence``
# (none is set), ``model_context_window_exceeded``, a missing or new value)
# is ``malformed`` (#572).
NORMAL_STOP_REASONS = frozenset({"end_turn"})


def _outcome(stop_reason: str | None) -> ProviderOutcome:
    if stop_reason == "refusal":
        return "refused"
    if stop_reason == "max_tokens":
        return "truncated"
    if stop_reason in NORMAL_STOP_REASONS:
        return "ok"
    return "malformed"


def _reply_text(message: Any) -> str:
    """The text blocks of the reply, in order (thinking blocks are skipped)."""
    parts = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    """The ``retry-after`` header in seconds, when it is a number."""
    try:
        value = exc.response.headers.get("retry-after")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(seconds) or seconds < 0:
        return None
    return seconds


def _now() -> float:
    return anyio.current_time()


async def _sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


def backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Seconds to wait before retry number ``attempt + 1``: full-jitter
    exponential backoff, but never less than the server's ``retry-after``."""
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2**attempt))
    delay = random.uniform(0, ceiling)  # nosec B311 - backoff jitter, not crypto
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class AnthropicVisionProvider:
    def __init__(self, spec: FeatureSpec) -> None:
        if spec.provider != "anthropic":
            raise ValueError(f"{spec.name} is not an Anthropic feature")
        self.spec = spec

    def request(self, image_bytes: bytes) -> dict[str, Any]:
        """The ``messages.create`` arguments: all from the spec and settings.

        Server-side ``fallbacks`` are deliberately not used: the usage ledger
        prices each call by ``spec.model``, and a fallback would be billed at
        another model's rates, so the recorded cost would be wrong.
        """
        max_books = settings.scan_max_books
        b64 = base64.standard_b64encode(image_bytes).decode()
        return {
            "model": self.spec.model,
            "max_tokens": self.spec.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT.format(max_books=max_books),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "output_config": {
                "effort": self.spec.effort,
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(output_schema(max_books)),
                },
            },
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": USER_TEXT},
                    ],
                }
            ],
        }

    def _client(self) -> anthropic.AsyncAnthropic:
        extra: dict[str, Any] = {}
        if settings.ai_anthropic_base_url:
            # Only with the local override (the load run's fake): never follow
            # a redirect, so a local endpoint cannot bounce the request, with
            # its API-key header and the photo, to another host. The default
            # client is unchanged.
            extra["http_client"] = anthropic.DefaultAsyncHttpxClient(
                follow_redirects=False
            )
        return anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.ai_anthropic_base_url or ANTHROPIC_BASE_URL,
            timeout=self.spec.timeout_s,
            # Our loop below is the only retry loop, so its count and the
            # deadline hold.
            max_retries=0,
            **extra,
        )

    async def identify(self, image_bytes: bytes) -> ProviderResult:
        if not settings.anthropic_api_key:
            # Fail closed, before any call: the feature is unavailable (503,
            # refunded) until the key is set.
            logger.error(
                "ANTHROPIC_API_KEY is not set but %s uses the anthropic provider",
                self.spec.name,
            )
            raise ScanUnavailableError("Claude provider is not configured")

        request = self.request(image_bytes)
        deadline = _now() + self.spec.timeout_s
        async with self._client() as client:
            message = await self._create_with_retries(client, request, deadline)

        stop_reason = getattr(message, "stop_reason", None)
        input_tokens, output_tokens, cache_read, cache_write = _usage(message)
        outcome = _outcome(stop_reason)
        candidates: list[BookCandidate] = []
        if outcome == "refused":
            logger.warning("Claude declined the scan: stop_reason=refusal")
        elif outcome == "malformed":
            # An unexpected stop reason: the reply is not a finished answer.
            _log_content("an unexpected stop_reason", _reply_text(message), stop_reason)
        else:
            text = _reply_text(message)
            if outcome == "truncated":
                _log_content("a truncated reply", text, stop_reason)
            parsed = parse_reply(text, stop_reason, settings.scan_max_books)
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
            cache_write_tokens=cache_write,
            outcome=outcome,
        )

    async def _create_with_retries(
        self,
        client: anthropic.AsyncAnthropic,
        request: dict[str, Any],
        deadline: float,
    ) -> Any:
        attempt = 0
        while True:
            try:
                return await client.messages.create(**request)
            except anthropic.APITimeoutError as exc:
                logger.warning("Claude request timed out")
                raise ProviderTimeoutError("Vision API timed out") from exc
            except anthropic.APIConnectionError as exc:
                if isinstance(exc.__cause__, httpx2.ConnectError):
                    logger.warning("Claude connection could not be established")
                    raise ProviderConnectError("Vision API unreachable") from exc
                logger.warning("Claude connection failed mid-request")
                raise
            except anthropic.APIStatusError as exc:
                status = exc.status_code
                if status not in RETRYABLE_STATUSES:
                    log = logger.error if 400 <= status < 500 else logger.warning
                    # A 4xx means our request is wrong (a bug signal). The
                    # error body is not logged: it may quote the request.
                    log(
                        "Claude request failed: status=%s type=%s",
                        status,
                        getattr(exc, "type", None) or "unknown",
                    )
                    raise ScanUnavailableError("Vision API returned an error") from exc
                if attempt >= MAX_RETRIES:
                    logger.warning(
                        "Claude still unavailable after %d retries: status=%s",
                        attempt,
                        status,
                    )
                    raise ScanUnavailableError("Vision API returned an error") from exc
                delay = backoff_delay(attempt, _retry_after(exc))
                if _now() + delay >= deadline:
                    logger.warning(
                        "Claude unavailable (status=%s); no time left to retry",
                        status,
                    )
                    raise ScanUnavailableError("Vision API returned an error") from exc
                attempt += 1
                logger.info(
                    "Claude status=%s; retry %d/%d in %.2fs",
                    status,
                    attempt,
                    MAX_RETRIES,
                    delay,
                )
                await _sleep(delay)
