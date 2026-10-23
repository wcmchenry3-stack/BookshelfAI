"""Pure helpers behind the provider-agreement and ISBN rules (#602 security
review round 2, findings 3 and 4)."""

import pytest

from app.services.enrichment import (
    _edition_isbns,
    isbn_13_to_10,
    normalise_title,
    records_agree,
)


def _doc(title="Dune", authors=("Frank Herbert",), isbns=()):
    return {"title": title, "author_name": list(authors), "isbn": list(isbns)}


def _volume(title="Dune", authors=("Frank Herbert",), isbns=()):
    return {
        "volumeInfo": {
            "title": title,
            "authors": list(authors),
            "industryIdentifiers": [
                {"type": "ISBN_13", "identifier": i} for i in isbns
            ],
        }
    }


class TestNormaliseTitle:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("The Hobbit", "hobbit"),
            ("  A   Tale of Two Cities ", "tale of two cities"),
            ("An Instance of the Fingerpost", "instance of the fingerpost"),
            ("Dune: Messiah!", "dune messiah"),
            ("THEORY of Everything", "theory of everything"),
            ("Straße", "strasse"),
            (None, ""),
            (7, ""),
        ],
    )
    def test_normalises(self, raw, expected):
        assert normalise_title(raw) == expected


class TestRecordsAgree:
    def test_shared_isbn(self):
        assert records_agree(
            _doc(title="Dune", isbns=["9780441013593", "0441013597"]),
            _volume(title="Something else", authors=["X"], isbns=["9780441013593"]),
        )

    def test_title_and_surname(self):
        assert records_agree(
            _doc(title="The Left Hand of Darkness", authors=["Ursula K. Le Guin"]),
            _volume(title="left hand of darkness", authors=["Ursula Le Guin"]),
        )

    def test_same_title_other_author(self):
        assert not records_agree(
            _doc(title="Collected Poems", authors=["Philip Larkin"]),
            _volume(title="Collected Poems", authors=["Sylvia Plath"]),
        )

    def test_same_author_other_title(self):
        assert not records_agree(
            _doc(title="Dune"), _volume(title="Dune Messiah", isbns=["9780000000002"])
        )

    def test_missing_author_is_not_agreement(self):
        assert not records_agree(_doc(authors=()), _volume(authors=()))

    def test_malformed_records(self):
        assert not records_agree({"title": None, "isbn": "x"}, {"volumeInfo": None})


class TestIsbn13To10:
    @pytest.mark.parametrize(
        ("isbn_13", "isbn_10"),
        [
            ("9780441013593", "0441013597"),
            ("9780306406157", "0306406152"),
            ("9780804429573", "080442957X"),
        ],
    )
    def test_converts(self, isbn_13, isbn_10):
        assert isbn_13_to_10(isbn_13) == isbn_10

    @pytest.mark.parametrize(
        "value", [None, "", "9791234567896", "978044101359", "97804410135X3"]
    )
    def test_no_isbn_10_form(self, value):
        assert isbn_13_to_10(value) is None


class TestEditionIsbns:
    def test_isbn_10_must_be_the_isbn_13s(self):
        confirmed = {"9780441013593", "0340960191"}
        assert _edition_isbns("9780441013593", "0340960191", confirmed) == (
            "9780441013593",
            None,
        )
        assert _edition_isbns("9780441013593", "0441013597", confirmed) == (
            "9780441013593",
            "0441013597",
        )

    def test_lowercase_check_digit(self):
        assert _edition_isbns("9780804429573", "080442957x", {"9780804429573"}) == (
            "9780804429573",
            "080442957X",
        )

    def test_isbn_10_alone(self):
        assert _edition_isbns(None, "0340960191", {"0340960191"}) == (
            None,
            "0340960191",
        )
        assert _edition_isbns("9780000000002", "0340960191", set()) == (None, None)
