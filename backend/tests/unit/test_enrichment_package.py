"""The ``app.services.enrichment`` package keeps the public surface of the
single module it replaced (#809), and each name lives in its owning module."""

import importlib

import pytest

from app.services import enrichment

PUBLIC_NAMES = {
    "ENRICHMENT_DEADLINE_S": "service",
    "MAX_CONCURRENT_ENRICHMENTS": "service",
    "PROVIDER_CALL_TIMEOUT_S": "service",
    "TITLE_SEARCH_RESULTS": "service",
    "UNKNOWN_AUTHOR": "service",
    "EnrichmentService": "service",
    "isbn_10_to_13": "isbn",
    "isbn_13_to_10": "isbn",
    "normalise_isbn_13": "isbn",
    "valid_isbn_13": "isbn",
    "normalise_text": "text",
    "normalise_title": "text",
    "reading_surname": "authors",
    "reading_match_score": "scoring",
    "records_agree": "scoring",
    "titles_agree": "titles",
}


def test_public_reexport_list():
    public = {name for name in enrichment.__all__ if not name.startswith("_")}
    assert public == set(PUBLIC_NAMES)


@pytest.mark.parametrize(("name", "module"), sorted(PUBLIC_NAMES.items()))
def test_each_name_is_the_owning_modules_object(name, module):
    owner = importlib.import_module(f"app.services.enrichment.{module}")
    assert getattr(enrichment, name) is getattr(owner, name)


@pytest.mark.parametrize("name", sorted(enrichment.__all__))
def test_every_exported_name_resolves(name):
    assert hasattr(enrichment, name)


def test_logger_name_is_unchanged_by_the_split():
    from app.services.enrichment import service

    assert service.logger.name == "app.services.enrichment"


def test_the_service_reads_its_constants_from_its_own_module(monkeypatch):
    """A patch must target ``enrichment.service``: the package attribute is
    only a re-export."""
    from app.services.enrichment import service

    monkeypatch.setattr(service, "PROVIDER_CALL_TIMEOUT_S", 0.5)
    assert service.PROVIDER_CALL_TIMEOUT_S == 0.5
    assert enrichment.PROVIDER_CALL_TIMEOUT_S != 0.5
