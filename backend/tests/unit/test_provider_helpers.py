"""Edge cases in the vision provider's parsing and error helpers that the
end-to-end provider tests do not reach."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.ai.providers import anthropic_vision as av
from app.ai.providers._common import MAX_TOKEN_COUNT, clean_isbn_10


class TestBookValidators:
    def test_title_must_be_a_string(self):
        with pytest.raises(ValidationError):
            av._Book(title=123, author="A")

    def test_author_is_stripped_and_blank_is_rejected(self):
        assert av._Book(title="T", author="  Ann  ").author == "Ann"
        with pytest.raises(ValidationError):
            av._Book(title="T", author="   ")

    def test_over_long_text_is_rejected(self):
        with pytest.raises(ValidationError):
            av._Book(title="x" * (av.MAX_TEXT_LENGTH + 1), author="A")

    @pytest.mark.parametrize("raw", [True, "0.9", None, [1], float("nan")])
    def test_unusable_confidence_falls_back_to_half(self, raw):
        assert av._Book(title="T", author="A", confidence=raw).confidence == 0.5

    @pytest.mark.parametrize("raw,expected", [(7, 1.0), (-3, 0.0), (0.25, 0.25)])
    def test_confidence_is_clamped_to_unit_range(self, raw, expected):
        assert av._Book(title="T", author="A", confidence=raw).confidence == expected

    def test_invalid_isbns_are_dropped_not_fatal(self):
        book = av._Book(title="T", author="A", isbn_13="123", isbn_10="123")
        assert book.isbn_13 is None and book.isbn_10 is None

    def test_valid_isbns_are_kept(self):
        book = av._Book(
            title="T", author="A", isbn_13="9780441013593", isbn_10="0441013597"
        )
        assert book.isbn_13 == "9780441013593"
        assert book.isbn_10 == "0441013597"

    def test_isbn_10_with_x_check_digit(self):
        assert clean_isbn_10("080442957X") == "080442957X"

    def test_isbn_10_bad_checksum(self):
        assert clean_isbn_10("0441013598") is None


class TestCompleteItems:
    def test_no_books_array_gives_empty(self):
        assert av._complete_items('{"other": []}') == []

    def test_cut_off_reply_keeps_only_whole_items(self):
        text = '{"books":[{"title":"A"},{"title":"B"},{"tit'
        assert av._complete_items(text) == [{"title": "A"}, {"title": "B"}]

    def test_closed_array_stops_at_the_bracket(self):
        text = '{"books": [ {"title":"A"} , ] trailing'
        assert av._complete_items(text) == [{"title": "A"}]

    def test_array_that_never_started_items(self):
        assert av._complete_items('{"books":[') == []


class TestUsage:
    def test_missing_usage_is_all_zero(self):
        assert av._usage(SimpleNamespace()) == (0, 0, 0, 0)

    def test_counts_are_clamped_and_junk_is_zero(self):
        usage = SimpleNamespace(
            input_tokens=10,
            output_tokens=-5,
            cache_read_input_tokens=True,
            cache_creation_input_tokens="7",
        )
        assert av._usage(SimpleNamespace(usage=usage)) == (10, 0, 0, 0)

    def test_huge_counts_are_capped(self):
        usage = SimpleNamespace(input_tokens=10**15)
        assert av._usage(SimpleNamespace(usage=usage))[0] == MAX_TOKEN_COUNT


class TestRetryAfter:
    @staticmethod
    def _exc(headers):
        return SimpleNamespace(response=SimpleNamespace(headers=headers))

    def test_numeric_header(self):
        assert av._retry_after(self._exc({"retry-after": "3.5"})) == 3.5

    def test_missing_header(self):
        assert av._retry_after(self._exc({})) is None

    def test_no_response_object(self):
        assert av._retry_after(SimpleNamespace()) is None

    @pytest.mark.parametrize("value", ["soon", "nan", "-1", ""])
    def test_unusable_values_are_ignored(self, value):
        assert av._retry_after(self._exc({"retry-after": value})) is None


class TestBackoff:
    def test_delay_never_exceeds_the_cap(self):
        for attempt in range(10):
            assert 0 <= av.backoff_delay(attempt, None) <= av.BACKOFF_CAP_S

    def test_server_retry_after_is_a_floor(self):
        assert av.backoff_delay(0, 30.0) == 30.0
