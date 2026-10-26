"""Unit tests for the scan correction flags (#631): what counts as an edit,
the pre-fill the server mirrors, the request and response fields, the
candidate index enrichment sets, and the endpoint wiring (the reference is
optional, a failure never fails the save). Real Postgres behaviour is in
tests/integration/test_scan_corrections_db.py; the authority checks in
tests/security/test_scan_corrections_authority.py."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.ai import scan_corrections
from app.ai.scan_corrections import Flags, Prefill, compare, prefill_from_candidate
from app.schemas.book import EnrichedBook, ScanResponse
from app.schemas.user_book import ScanReference, UnverifiedBookCreate
from app.services.book_identifier import BookCandidate
from app.services.enrichment import EnrichmentService

ISBN_13 = "9780306406157"
ISBN_13_HYPHENS = "978-0-306-40615-7"
ISBN_10 = "0306406152"
OTHER_ISBN_13 = "9780441013593"

PREFILL = Prefill(
    title="The Left Hand of Darkness", author="Ursula K. Le Guin", isbn=None
)


def _flags(prefill=PREFILL, **saved):
    fields = {"title": prefill.title, "author": prefill.author, "isbn": prefill.isbn}
    fields.update(saved)
    return compare(prefill, **fields)


# ── What counts as an edit ───────────────────────────────────────────────────


def test_identical_values_are_not_edits():
    assert _flags() == Flags(False, False, False)
    assert _flags().any_edited is False


def test_identical_values_with_an_isbn_are_not_edits():
    prefill = Prefill(title="Dune", author="Frank Herbert", isbn=ISBN_13)
    assert _flags(prefill) == Flags(False, False, False)


@pytest.mark.parametrize(
    "saved, expected",
    [
        ({"title": "The Lathe of Heaven"}, Flags(True, False, False)),
        ({"author": "Octavia E. Butler"}, Flags(False, True, False)),
        ({"isbn": ISBN_13}, Flags(False, False, True)),
    ],
)
def test_each_single_field_change_sets_only_its_flag(saved, expected):
    flags = _flags(**saved)
    assert flags == expected
    assert flags.any_edited is True


@pytest.mark.parametrize(
    "title",
    [
        "the left hand of darkness",
        "THE LEFT HAND OF DARKNESS",
        "  The   Left Hand of Darkness ",
        "The Left-Hand of Darkness.",
        "The Left Hand of Darkness!",
        "Left Hand of Darkness",  # a leading article is not an edit
    ],
)
def test_title_case_whitespace_and_punctuation_are_not_edits(title):
    assert _flags(title=title).title_edited is False


@pytest.mark.parametrize(
    "author",
    [
        "ursula k le guin",
        "URSULA K. LE GUIN",
        " Ursula  K.  Le Guin ",
        "Ursula K Le-Guin",
    ],
)
def test_author_case_whitespace_and_punctuation_are_not_edits(author):
    assert _flags(author=author).author_edited is False


def test_punctuation_only_text_still_compares():
    prefill = Prefill(title="!!!", author="???", isbn=None)
    assert _flags(prefill).any_edited is False
    assert _flags(prefill, title="...").title_edited is True


@pytest.mark.parametrize("saved", [ISBN_13, ISBN_13_HYPHENS, ISBN_10, "0-306-40615-2"])
def test_isbn_hyphens_spaces_and_isbn_10_form_are_not_edits(saved):
    prefill = Prefill(title="T", author="A", isbn=ISBN_13_HYPHENS)
    assert _flags(prefill, isbn=saved).isbn_edited is False
    prefill = Prefill(title="T", author="A", isbn=ISBN_13)
    assert _flags(prefill, isbn=saved).isbn_edited is False


def test_an_isbn_added_is_an_edit():
    assert _flags(isbn=ISBN_13).isbn_edited is True


@pytest.mark.parametrize("saved", [None, "", "   "])
def test_an_isbn_removed_is_an_edit(saved):
    prefill = Prefill(title="T", author="A", isbn=ISBN_13)
    assert _flags(prefill, isbn=saved).isbn_edited is True


def test_an_isbn_changed_is_an_edit():
    prefill = Prefill(title="T", author="A", isbn=ISBN_13)
    assert _flags(prefill, isbn=OTHER_ISBN_13).isbn_edited is True


def test_an_unparseable_stored_isbn_still_compares():
    prefill = Prefill(title="T", author="A", isbn="12-345")
    assert _flags(prefill, isbn="12345").isbn_edited is False
    assert _flags(prefill, isbn=ISBN_13).isbn_edited is True


# ── The pre-fill the server mirrors ──────────────────────────────────────────


def test_prefill_is_the_readings_title_and_author_and_no_isbn():
    """As the app pre-fills a book no provider matched: the reading's title
    and the author string as read (several authors stay one string), and
    an empty ISBN (enrichment keeps provider-confirmed ISBNs only)."""
    candidate = BookCandidate(
        title="Good Omens",
        author="Terry Pratchett, Neil Gaiman",
        confidence=0.8,
        isbn_13=ISBN_13,
        isbn_10=ISBN_10,
    )
    prefill = prefill_from_candidate(candidate)
    assert prefill == Prefill(
        title="Good Omens", author="Terry Pratchett, Neil Gaiman", isbn=None
    )
    # Saving the form untouched is not an edit, the read ISBN included.
    assert compare(
        prefill, title="Good Omens", author="Terry Pratchett, Neil Gaiman", isbn=None
    ) == Flags(False, False, False)
    # Dropping a co-author is.
    assert (
        compare(
            prefill, title="Good Omens", author="Terry Pratchett", isbn=None
        ).author_edited
        is True
    )


def test_prefill_is_cut_like_the_form():
    candidate = BookCandidate(title="x" * 500, author="y" * 500, confidence=0.5)
    prefill = prefill_from_candidate(candidate)
    assert len(prefill.title) == 500 and len(prefill.author) == 500


# ── Request and response fields ──────────────────────────────────────────────


def test_scan_reference_is_optional():
    assert UnverifiedBookCreate(title="T", author="A").scan_reference is None


def test_scan_reference_accepts_a_uuid_and_an_index():
    rid = uuid.uuid4()
    req = UnverifiedBookCreate(
        title="T",
        author="A",
        scan_reference={"scan_record_id": str(rid), "candidate_index": 2},
    )
    assert req.scan_reference == ScanReference(scan_record_id=rid, candidate_index=2)


@pytest.mark.parametrize(
    "ref",
    [
        {"scan_record_id": "not-a-uuid", "candidate_index": 0},
        {"scan_record_id": str(uuid.uuid4()), "candidate_index": -1},
        {"scan_record_id": str(uuid.uuid4()), "candidate_index": 1000},
        {"scan_record_id": str(uuid.uuid4()), "candidate_index": "x"},
        {"scan_record_id": str(uuid.uuid4())},
        {"candidate_index": 0},
        {
            "scan_record_id": str(uuid.uuid4()),
            "candidate_index": 0,
            "title_edited": False,
        },
        {"scan_record_id": str(uuid.uuid4()), "candidate_index": 0, "title": "Dune"},
    ],
)
def test_scan_reference_rejects_bad_values_and_extra_fields(ref):
    with pytest.raises(ValidationError):
        UnverifiedBookCreate(title="T", author="A", scan_reference=ref)


def test_scan_response_omits_a_missing_scan_record_id():
    assert "scan_record_id" not in ScanResponse().model_dump()
    assert "scan_record_id" not in ScanResponse().model_dump(mode="json")
    rid = uuid.uuid4()
    assert ScanResponse(scan_record_id=rid).model_dump(mode="json")[
        "scan_record_id"
    ] == str(rid)


def test_enriched_book_candidate_index_is_non_negative():
    assert EnrichedBook(title="T", author="A", confidence=0.5).candidate_index is None
    with pytest.raises(ValidationError):
        EnrichedBook(title="T", author="A", confidence=0.5, candidate_index=-1)


async def test_enrichment_tags_each_book_with_its_candidate_index():
    """A candidate whose enrichment fails is dropped, so the index is the
    candidate's, not the book's position."""
    candidates = [
        BookCandidate(title=t, author="A", confidence=0.5) for t in ("a", "b", "c")
    ]
    service = EnrichmentService()

    async def one(candidate):
        if candidate.title == "b":
            raise RuntimeError("lookup failed")
        return EnrichedBook(title=candidate.title, author="A", confidence=0.5)

    with patch.object(service, "_enrich_one", side_effect=one):
        books = await service.enrich(candidates, limit=3)
    assert [(b.title, b.candidate_index) for b in books] == [("a", 0), ("c", 2)]


# ── Recording ────────────────────────────────────────────────────────────────


async def test_record_quietly_never_raises_and_rolls_back():
    db = AsyncMock()
    with patch.object(
        scan_corrections, "record", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        await scan_corrections.record_quietly(db, user_id=uuid.uuid4())
    db.rollback.assert_awaited_once()


async def test_record_quietly_survives_a_failed_rollback():
    db = AsyncMock()
    db.rollback.side_effect = RuntimeError("gone")
    with patch.object(
        scan_corrections, "record", AsyncMock(side_effect=RuntimeError("db down"))
    ):
        await scan_corrections.record_quietly(db, user_id=uuid.uuid4())


@pytest.mark.parametrize("index", [1, 5])
async def test_record_ignores_an_index_out_of_range(index):
    found = AsyncMock(
        return_value=type(
            "R",
            (),
            {
                "candidates": [BookCandidate(title="T", author="A", confidence=0.5)],
                "feature": "scan.standard",
                "tier": "free",
                "model": "m",
            },
        )()
    )
    db = AsyncMock()
    with patch.object(scan_corrections.scan_records, "lookup_by_id", found):
        assert (
            await scan_corrections.record(
                db,
                user_id=uuid.uuid4(),
                scan_record_id=uuid.uuid4(),
                candidate_index=index,
                title="T",
                author="A",
                isbn=None,
            )
            is False
        )
    db.execute.assert_not_awaited()


async def test_record_ignores_a_record_it_cannot_find():
    db = AsyncMock()
    with patch.object(
        scan_corrections.scan_records, "lookup_by_id", AsyncMock(return_value=None)
    ):
        assert (
            await scan_corrections.record(
                db,
                user_id=uuid.uuid4(),
                scan_record_id=uuid.uuid4(),
                candidate_index=0,
                title="T",
                author="A",
                isbn=None,
            )
            is False
        )
    db.execute.assert_not_awaited()


# ── Never failing the scan or the ledger run ─────────────────────────────────


async def test_scan_record_id_read_failure_gives_no_reference():
    from app.api import scan as scan_api

    db = AsyncMock()
    with patch.object(
        scan_api.scan_records, "record_id", AsyncMock(side_effect=RuntimeError("x"))
    ):
        found = await scan_api._saved_record_id(
            db,
            fingerprint="f" * 64,
            user_id=uuid.uuid4(),
            ai_created_at=datetime.now(UTC),
        )
    assert found is None
    db.rollback.assert_awaited()


async def test_scan_record_id_needs_a_saved_step():
    from app.api import scan as scan_api

    db = AsyncMock()
    assert (
        await scan_api._saved_record_id(
            db, fingerprint=None, user_id=uuid.uuid4(), ai_created_at=None
        )
        is None
    )
    db.execute.assert_not_awaited()


@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_correction_purge_failure_never_fails_the_usage_rollup(
    monkeypatch, rollback_fails
):
    from app.ai import ledger

    monkeypatch.setattr(ledger, "_rollup_batch", AsyncMock(return_value=5))
    monkeypatch.setattr(ledger, "_purge_daily", AsyncMock(return_value=2))
    monkeypatch.setattr(
        ledger, "_purge_scan_corrections", AsyncMock(side_effect=RuntimeError("x"))
    )
    db = AsyncMock()
    if rollback_fails:
        db.rollback.side_effect = RuntimeError("gone")
    assert await ledger.rollup_and_purge(db) == (5, 2)
    db.rollback.assert_awaited_once()
