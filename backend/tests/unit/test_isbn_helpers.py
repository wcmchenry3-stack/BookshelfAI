"""Unit tests for the shared ISBN cleaning helpers (#746)."""

import pytest

from app.ai.providers._common import clean_isbn_10, clean_isbn_13

# The two ISBN-13s the model invented on 2026-10-28 (#743): bad check digits.
INVENTED_13 = ["9780786412345", "9780817354368"]


class TestCleanIsbn13:
    def test_valid_is_returned(self):
        assert clean_isbn_13("9780441013593") == "9780441013593"

    def test_hyphens_and_spaces_are_stripped(self):
        assert clean_isbn_13("978-0-441-01359-3") == "9780441013593"
        assert clean_isbn_13(" 978 0441 013593 ") == "9780441013593"

    @pytest.mark.parametrize("value", INVENTED_13)
    def test_bad_check_digit_is_none(self, value):
        assert clean_isbn_13(value) is None

    @pytest.mark.parametrize("value", ["", "978044101359", "97804410135933", "abc"])
    def test_wrong_shape_is_none(self, value):
        assert clean_isbn_13(value) is None

    def test_non_ascii_digits_are_none(self):
        assert clean_isbn_13("٩٧٨٠٤٤١٠١٣٥٩٣") is None

    @pytest.mark.parametrize("value", [None, 9780441013593, ["9780441013593"], {}])
    def test_non_string_is_none(self, value):
        assert clean_isbn_13(value) is None


class TestCleanIsbn10:
    def test_valid_is_returned(self):
        assert clean_isbn_10("0441013597") == "0441013597"

    def test_hyphens_are_stripped(self):
        assert clean_isbn_10("0-441-01359-7") == "0441013597"

    def test_x_check_character_is_accepted_and_upper_cased(self):
        assert clean_isbn_10("080442957X") == "080442957X"
        assert clean_isbn_10("080442957x") == "080442957X"

    def test_bad_check_digit_is_none(self):
        assert clean_isbn_10("0441013598") is None

    def test_x_only_allowed_last(self):
        assert clean_isbn_10("X441013597") is None

    def test_non_ascii_digits_are_none(self):
        assert clean_isbn_10("٠٤٤١٠١٣٥٩٧") is None

    @pytest.mark.parametrize("value", [None, 441013597, ["0441013597"]])
    def test_non_string_is_none(self, value):
        assert clean_isbn_10(value) is None
