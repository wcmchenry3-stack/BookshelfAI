"""Claude vision provider (#532): SDK client mocked, never a real model."""

import dataclasses
import json
import logging
from unittest.mock import AsyncMock, patch

import anthropic
import pytest

from app.ai import prices
from app.ai.models_allowlist import ALLOWED_MODELS
from app.ai.providers import anthropic_vision
from app.ai.providers.anthropic_vision import (
    ANTHROPIC_BASE_URL,
    MAX_RETRIES,
    AnthropicVisionProvider,
    backoff_delay,
    output_schema,
    parse_books,
)
from app.ai.providers.base import ProviderConnectError, ProviderTimeoutError
from app.ai.providers.openai_vision import SYSTEM_PROMPT
from app.ai.registry import FEATURES, PROVIDER_MODELS, default_model
from app.core.config import settings
from app.services.book_identifier import ScanUnavailableError
from tests._claude_fake import (
    connect_error,
    dropped_error,
    fake_claude,
    message,
    status_error,
    timeout_error,
)

IMAGE = b"\xff\xd8\xff-fake-jpeg"
# Owner decision on #548 (option a): these fixed IDs, nothing else.
APPROVED_ANTHROPIC_MODELS = frozenset({"claude-haiku-5-5", "claude-sonnet-5-5"})
DUNE = {
    "title": "Dune",
    "author": "Frank Herbert",
    "confidence": 0.97,
    "isbn_13": "9780441013593",
    "isbn_10": "0441013597",
}
GATSBY = {
    "title": "The Great Gatsby",
    "author": "F. Scott Fitzgerald",
    "confidence": 0.9,
    "isbn_13": "9780743273565",
    "isbn_10": None,
}


def _spec(feature: str = "scan.standard"):
    return dataclasses.replace(
        FEATURES[feature],
        provider="anthropic",
        model=default_model(feature, "anthropic"),
    )


@pytest.fixture
def provider():
    return AnthropicVisionProvider(_spec())


@pytest.fixture
def clock():
    """A fake clock: _now() returns ``clock.t``; _sleep advances it."""

    class Clock:
        def __init__(self) -> None:
            self.t = 1_000.0
            self.sleeps: list[float] = []

    c = Clock()

    async def sleep(seconds):
        c.sleeps.append(seconds)
        c.t += seconds

    with (
        patch.object(anthropic_vision, "_now", side_effect=lambda: c.t),
        patch.object(anthropic_vision, "_sleep", side_effect=sleep),
    ):
        yield c


# ---------------------------------------------------------------------------
# Model IDs (#547, #548)
# ---------------------------------------------------------------------------


class TestModelIds:
    def test_allowlist_has_exactly_the_approved_anthropic_ids(self):
        assert ALLOWED_MODELS["anthropic"] == APPROVED_ANTHROPIC_MODELS

    def test_registry_anthropic_models_are_approved(self):
        used = {models["anthropic"] for models in PROVIDER_MODELS.values()}
        assert used <= APPROVED_ANTHROPIC_MODELS

    @pytest.mark.parametrize("model", sorted(ALLOWED_MODELS["anthropic"]))
    def test_no_moving_names(self, model):
        assert not model.endswith("-latest")
        assert "latest" not in model
        assert model in APPROVED_ANTHROPIC_MODELS

    def test_tier_models(self):
        assert default_model("scan.standard", "anthropic") == "claude-haiku-5-5"
        assert default_model("scan.enhanced", "anthropic") == "claude-sonnet-5-5"

    def test_default_provider_is_still_openai(self):
        assert {s.provider for s in FEATURES.values()} == {"openai"}

    def test_effort_per_tier(self):
        assert FEATURES["scan.standard"].effort == "low"
        assert FEATURES["scan.enhanced"].effort == "medium"

    @pytest.mark.parametrize("model", sorted(APPROVED_ANTHROPIC_MODELS))
    def test_every_anthropic_model_is_priced_with_cache_writes(self, model):
        price = prices.price_for(model, prices.CURRENT_PRICE_VERSION)
        # Claude bills cache writes (1.25x input), unlike OpenAI.
        assert price.cache_write == price.input * 5 // 4
        assert price.cache_read == price.input // 10

    def test_rejects_non_anthropic_spec(self):
        with pytest.raises(ValueError):
            AnthropicVisionProvider(FEATURES["scan.standard"])


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


class TestRequest:
    async def test_request_comes_from_the_spec(self, provider):
        with fake_claude(message(books=[DUNE])) as (_client_cls, create):
            await provider.identify(IMAGE)

        kwargs = create.call_args.kwargs
        assert kwargs["model"] == "claude-haiku-5-5"
        assert kwargs["max_tokens"] == provider.spec.max_tokens
        assert kwargs["output_config"]["effort"] == "low"
        # Never sent: rejected by these models, and not ours to tune.
        for absent in ("temperature", "top_p", "top_k", "thinking", "tools"):
            assert absent not in kwargs
        assert [m["role"] for m in kwargs["messages"]] == ["user"]

    async def test_system_prompt_is_the_openai_wording_and_cached(self, provider):
        with fake_claude(message()) as (_cls, create):
            await provider.identify(IMAGE)

        system = create.call_args.kwargs["system"]
        assert system == [
            {
                "type": "text",
                "text": SYSTEM_PROMPT.format(max_books=settings.scan_max_books),
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_output_schema(self, provider):
        with fake_claude(message()) as (_cls, create):
            await provider.identify(IMAGE)

        fmt = create.call_args.kwargs["output_config"]["format"]
        assert fmt["type"] == "json_schema"
        sent = fmt["schema"]
        assert sent == anthropic.transform_schema(
            output_schema(settings.scan_max_books)
        )
        books = sent["properties"]["books"]
        item = books["items"]
        assert set(item["properties"]) == {
            "title",
            "author",
            "confidence",
            "isbn_13",
            "isbn_10",
        }
        assert item["additionalProperties"] is False
        # maxItems is not enforced by the API; the SDK carries it in the
        # description and the parser enforces it.
        assert f"maxItems: {settings.scan_max_books}" in books["description"]

    def test_source_schema_caps_books(self):
        schema = output_schema(15)
        assert schema["properties"]["books"]["maxItems"] == 15

    async def test_image_is_base64_jpeg(self, provider):
        with fake_claude(message()) as (_cls, create):
            await provider.identify(IMAGE)

        content = create.call_args.kwargs["messages"][0]["content"]
        assert content[0]["type"] == "image"
        assert content[0]["source"]["media_type"] == "image/jpeg"
        assert content[0]["source"]["type"] == "base64"
        import base64

        assert base64.b64decode(content[0]["source"]["data"]) == IMAGE

    async def test_client_has_sdk_retries_off_and_pinned_host(self, provider):
        with fake_claude(message()) as (client_cls, _create):
            await provider.identify(IMAGE)

        client_cls.assert_called_once_with(
            api_key="test-anthropic-key",
            base_url=ANTHROPIC_BASE_URL,
            timeout=provider.spec.timeout_s,
            max_retries=0,
        )

    async def test_enhanced_uses_sonnet_at_medium_effort(self):
        provider = AnthropicVisionProvider(_spec("scan.enhanced"))
        with fake_claude(message()) as (_cls, create):
            await provider.identify(IMAGE)
        assert create.call_args.kwargs["model"] == "claude-sonnet-5-5"
        assert create.call_args.kwargs["output_config"]["effort"] == "medium"

    async def test_missing_key_fails_closed_without_a_call(self, provider, caplog):
        with (
            fake_claude(message(), api_key="") as (client_cls, create),
            caplog.at_level(logging.ERROR),
            pytest.raises(ScanUnavailableError),
        ):
            await provider.identify(IMAGE)
        client_cls.assert_not_called()
        create.assert_not_awaited()
        assert "ANTHROPIC_API_KEY is not set" in caplog.text


# ---------------------------------------------------------------------------
# Result: parsing, outcome, usage
# ---------------------------------------------------------------------------


class TestResult:
    async def test_parses_books_and_usage(self, provider):
        reply = message(
            books=[DUNE, GATSBY],
            input_tokens=1_500,
            output_tokens=210,
            cache_read=480,
            cache_write=0,
            thinking=True,
        )
        with fake_claude(reply):
            result = await provider.identify(IMAGE)

        assert result.outcome == "ok"
        assert [c.title for c in result.candidates] == ["Dune", "The Great Gatsby"]
        assert result.candidates[0].isbn_13 == "9780441013593"
        assert result.candidates[0].isbn_10 == "0441013597"
        assert result.candidates[0].confidence == 0.97
        assert (
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
        ) == (1_500, 210, 480, 0)

    async def test_cache_write_tokens_recorded(self, provider):
        with fake_claude(message(cache_read=None, cache_write=900)):
            result = await provider.identify(IMAGE)
        assert result.cache_write_tokens == 900
        assert result.cache_read_tokens == 0

    async def test_refusal_is_empty_and_refused(self, provider):
        reply = message(books=[DUNE], stop_reason="refusal")
        with fake_claude(reply):
            result = await provider.identify(IMAGE)
        assert result.outcome == "refused"
        assert result.candidates == []
        # Still billed: usage is recorded for the ledger.
        assert result.input_tokens == 1_200

    async def test_truncated_keeps_complete_books(self, provider):
        full = json.dumps({"books": [DUNE, GATSBY]})
        cut = full[: full.index("The Great") + 4]
        with fake_claude(message(cut, stop_reason="max_tokens")):
            result = await provider.identify(IMAGE)
        assert result.outcome == "truncated"
        assert [c.title for c in result.candidates] == ["Dune"]

    async def test_truncated_with_no_valid_json_is_empty(self, provider):
        with fake_claude(message('{"boo', stop_reason="max_tokens")):
            result = await provider.identify(IMAGE)
        assert result.outcome == "truncated"
        assert result.candidates == []

    async def test_empty_reply_is_ok_and_empty(self, provider):
        with fake_claude(message("")):
            result = await provider.identify(IMAGE)
        assert result.outcome == "ok"
        assert result.candidates == []

    async def test_garbled_usage_gives_zeros(self, provider):
        reply = message()
        reply.usage.input_tokens = -5
        reply.usage.output_tokens = True
        with fake_claude(reply):
            result = await provider.identify(IMAGE)
        assert (result.input_tokens, result.output_tokens) == (0, 0)


class TestParsing:
    def _parse(self, books, max_books=15):
        return parse_books(json.dumps({"books": books}), "end_turn", max_books)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("isbn_13", "9780441013594"),  # bad checksum
            ("isbn_13", "978044101359"),  # 12 digits
            ("isbn_13", "97804410135933"),  # 14 digits
            ("isbn_13", "978044101359X"),
            ("isbn_13", "٩٧٨٠٤٤١٠١٣٥٩٣"),  # non-ASCII digits
            ("isbn_10", "0441013598"),  # bad checksum
            ("isbn_10", "044101359"),
            ("isbn_10", "X441013597"),
            ("isbn_13", 9780441013593),  # not a string
        ],
    )
    def test_invalid_isbn_dropped_book_kept(self, field, value):
        [book] = self._parse([{**DUNE, field: value}])
        assert getattr(book, field) is None
        assert (book.title, book.author) == ("Dune", "Frank Herbert")

    def test_valid_isbns_normalised(self):
        [book] = self._parse(
            [{**DUNE, "isbn_13": "978-0-441-01359-3", "isbn_10": "0-8044-2957-x"}]
        )
        assert book.isbn_13 == "9780441013593"
        assert book.isbn_10 == "080442957X"

    @pytest.mark.parametrize(
        "item",
        [
            {"author": "A", "confidence": 0.5},
            {"title": "T", "confidence": 0.5},
            {"title": "", "author": "A"},
            {"title": "T", "author": "  "},
            {"title": "x" * 501, "author": "A"},
            {"title": 5, "author": "A"},
            "Dune",
            None,
        ],
    )
    def test_invalid_books_skipped(self, item):
        assert self._parse([item, DUNE])[0].title == "Dune"
        assert len(self._parse([item, DUNE])) == 1

    @pytest.mark.parametrize(
        "value,expected", [(1.7, 1.0), (-1, 0.0), ("high", 0.5), (True, 0.5)]
    )
    def test_confidence_clamped(self, value, expected):
        [book] = self._parse([{**DUNE, "confidence": value}])
        assert book.confidence == expected

    def test_caps_and_dedupes(self):
        books = [{**DUNE, "title": f"Book {i}"} for i in range(20)]
        assert len(self._parse(books, max_books=15)) == 15
        assert len(self._parse([DUNE, {**DUNE, "title": " dune "}])) == 1

    @pytest.mark.parametrize("text", ["not json", "[]", '{"books": "x"}', "null"])
    def test_unparseable_is_empty(self, text):
        assert parse_books(text, "end_turn", 15) == []


# ---------------------------------------------------------------------------
# Retries and error mapping
# ---------------------------------------------------------------------------


class TestRetries:
    @pytest.mark.parametrize("status", [429, 500, 503, 529])
    async def test_retries_then_succeeds(self, provider, clock, status):
        with fake_claude(
            status_error(status), status_error(status), message(books=[DUNE])
        ) as (_cls, create):
            result = await provider.identify(IMAGE)
        assert create.await_count == 3
        assert len(clock.sleeps) == 2
        assert [c.title for c in result.candidates] == ["Dune"]

    @pytest.mark.parametrize("status", [429, 529])
    async def test_gives_up_after_two_retries(self, provider, clock, status):
        with (
            fake_claude(*[status_error(status)] * 5) as (_cls, create),
            pytest.raises(ScanUnavailableError) as exc_info,
        ):
            await provider.identify(IMAGE)
        assert create.await_count == MAX_RETRIES + 1 == 3
        assert len(clock.sleeps) == 2
        assert not isinstance(exc_info.value, ProviderTimeoutError)

    async def test_backoff_grows_exponentially_with_jitter(self, provider, clock):
        with (
            patch.object(
                anthropic_vision.random, "uniform", side_effect=lambda a, b: b
            ) as uniform,
            fake_claude(status_error(429), status_error(529), message()),
        ):
            await provider.identify(IMAGE)
        assert clock.sleeps == [0.5, 1.0]
        assert [c.args for c in uniform.call_args_list] == [(0, 0.5), (0, 1.0)]

    def test_backoff_is_capped(self):
        with patch.object(
            anthropic_vision.random, "uniform", side_effect=lambda a, b: b
        ):
            assert backoff_delay(10, None) == anthropic_vision.BACKOFF_CAP_S

    async def test_honours_retry_after(self, provider, clock):
        with fake_claude(status_error(429, retry_after="3"), message()):
            await provider.identify(IMAGE)
        assert clock.sleeps[0] >= 3.0

    @pytest.mark.parametrize("header", ["soon", "-1", "nan"])
    async def test_ignores_bad_retry_after(self, provider, clock, header):
        with fake_claude(status_error(429, retry_after=header), message()):
            await provider.identify(IMAGE)
        assert 0 <= clock.sleeps[0] <= anthropic_vision.BACKOFF_BASE_S

    async def test_never_sleeps_past_the_deadline(self, provider, clock):
        # retry-after longer than the whole budget: give up now, no sleep.
        too_long = str(provider.spec.timeout_s + 1)
        with (
            fake_claude(status_error(429, retry_after=too_long), message()) as (
                _cls,
                create,
            ),
            pytest.raises(ScanUnavailableError),
        ):
            await provider.identify(IMAGE)
        assert clock.sleeps == []
        assert create.await_count == 1

    async def test_deadline_counts_time_already_spent(self, provider, clock):
        async def slow_fail(**_kwargs):
            clock.t += provider.spec.timeout_s - 0.1
            raise status_error(503)

        with (
            # Backoff at its ceiling (0.5 s), more than the 0.1 s left.
            patch.object(
                anthropic_vision.random, "uniform", side_effect=lambda a, b: b
            ),
            fake_claude() as (_cls, create),
            pytest.raises(ScanUnavailableError),
        ):
            create.side_effect = slow_fail
            await provider.identify(IMAGE)
        assert create.await_count == 1
        assert sum(clock.sleeps) == 0

    @pytest.mark.parametrize("status", [400, 401, 404, 413, 422])
    async def test_no_retry_on_other_4xx_and_logs_error(
        self, provider, clock, caplog, status
    ):
        with (
            caplog.at_level(logging.ERROR, logger=anthropic_vision.__name__),
            fake_claude(status_error(status), message()) as (_cls, create),
            pytest.raises(ScanUnavailableError),
        ):
            await provider.identify(IMAGE)
        assert create.await_count == 1
        assert clock.sleeps == []
        assert f"status={status}" in caplog.text
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    @pytest.mark.parametrize("status", [502, 504])
    async def test_other_5xx_not_retried(self, provider, clock, status):
        with (
            fake_claude(status_error(status), message()) as (_cls, create),
            pytest.raises(ScanUnavailableError),
        ):
            await provider.identify(IMAGE)
        assert create.await_count == 1

    async def test_timeout_maps_to_provider_timeout(self, provider, clock):
        with (
            fake_claude(timeout_error(), message()) as (_cls, create),
            pytest.raises(ProviderTimeoutError),
        ):
            await provider.identify(IMAGE)
        assert create.await_count == 1

    async def test_never_connected_maps_to_refundable_error(self, provider, clock):
        with (
            pytest.raises(ProviderConnectError) as exc_info,
            fake_claude(connect_error()),
        ):
            await provider.identify(IMAGE)
        assert isinstance(exc_info.value, ScanUnavailableError)

    async def test_dropped_connection_propagates(self, provider, clock):
        # May have been billed: not a ScanUnavailableError, so /scan keeps
        # the reservation (as an httpx transport error does for OpenAI).
        with (
            pytest.raises(anthropic.APIConnectionError) as exc_info,
            fake_claude(dropped_error()),
        ):
            await provider.identify(IMAGE)
        assert not isinstance(exc_info.value, ScanUnavailableError)


# ---------------------------------------------------------------------------
# Logging: never the content
# ---------------------------------------------------------------------------


class TestLogging:
    SECRET_TITLE = "Zxq Secret Shelf Title"

    @pytest.mark.parametrize(
        "reply",
        [
            message(f'{{"books": [{{"title": "{SECRET_TITLE}", "auth'),
            message(
                f'{{"books": [{{"title": "{SECRET_TITLE}", "auth',
                stop_reason="max_tokens",
            ),
            message(f"not json {SECRET_TITLE}"),
            message(
                json.dumps({"books": [{**DUNE, "title": SECRET_TITLE}]}),
                stop_reason="refusal",
            ),
        ],
    )
    async def test_logs_only_length_hash_and_stop_reason(self, provider, caplog, reply):
        with caplog.at_level(logging.DEBUG), fake_claude(reply):
            await provider.identify(IMAGE)
        assert self.SECRET_TITLE not in caplog.text
        assert "Zxq" not in caplog.text
        assert "test-anthropic-key" not in caplog.text
        assert "stop_reason=" in caplog.text
        if reply.stop_reason != "refusal":
            assert "length=" in caplog.text and "sha256=" in caplog.text

    async def test_error_body_and_key_never_logged(self, provider, clock, caplog):
        error = status_error(400)
        with (
            caplog.at_level(logging.DEBUG),
            fake_claude(error),
            pytest.raises(ScanUnavailableError),
        ):
            await provider.identify(IMAGE)
        assert "status=400" in caplog.text
        assert "test-anthropic-key" not in caplog.text
        assert IMAGE.decode("latin-1") not in caplog.text


def test_create_is_the_only_sdk_call(provider):
    """The adapter calls messages.create only (no batches, no beta)."""
    import inspect

    source = inspect.getsource(anthropic_vision)
    assert "client.messages.create(" in source
    assert ".beta." not in source
    assert "batches" not in source


async def test_async_mock_is_used():
    # Guard: the shared fake really replaces the SDK class.
    with fake_claude(message()) as (client_cls, create):
        assert anthropic_vision.anthropic.AsyncAnthropic is client_cls
        assert isinstance(create, AsyncMock)
