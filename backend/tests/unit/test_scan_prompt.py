"""The scan system prompt (shared by the OpenAI and Claude adapters) asks for
an ISBN only when one is printed and legible (#746).

It used to tell the model to supply an ISBN "whenever you can identify the
book", and the model invented ISBNs that fail the check digit (both ISBNs
logged for #743). Changing this wording affects recognition: re-run the AI
eval (docs/testing.md, "AI scan evaluation") whenever it changes.
"""

from app.ai.providers.openai_vision import SYSTEM_PROMPT


def _prompt() -> str:
    return SYSTEM_PROMPT.format(max_books=15)


def test_asks_for_an_isbn_only_when_printed_and_legible():
    prompt = _prompt()
    assert "only if a 13-digit ISBN is printed and legible in the photo" in prompt
    assert "only if a 10-digit ISBN is printed and legible in the photo" in prompt
    assert "Never guess or recall an ISBN" in prompt


def test_no_longer_asks_for_an_isbn_whenever_the_book_is_identified():
    prompt = _prompt()
    assert "at least one ISBN" not in prompt
    assert "any edition is fine" not in prompt


def test_still_formats_and_keeps_the_json_contract():
    prompt = _prompt()
    assert "up to 15 books" in prompt
    assert '{"books": [...]}' in prompt
    assert "isbn_13" in prompt and "isbn_10" in prompt


def test_the_example_shows_no_isbn():
    # A one-shot example primes the output: showing Dune's real ISBN invited
    # the model to recall valid ISBNs it cannot see, which the check digit
    # cannot catch (#746 review).
    example = _prompt().rsplit("Example:", 1)[1]
    assert '"isbn_13":null' in example and '"isbn_10":null' in example
    assert "9780441013593" not in example
