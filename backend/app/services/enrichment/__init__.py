"""EnrichmentService — merges Open Library + Google Books into EnrichedBook.

The candidate (the vision model's reading of the photo, or a search hit) is
only a search key. Which records are looked at (#743): with an ISBN-13, each
provider is first looked up by it, and a record found that way is used only
when it fits the reading (:func:`reading_match_score`: agreeing titles, and
a compatible author when both name one), since the model's ISBN is often
misread or invented. For each provider that has no such record (and for a
candidate without an ISBN-13) the provider is searched by title and author
surname, and the best fitting of up to ``TITLE_SEARCH_RESULTS`` records is
used; a record that fits nothing is never used. At most four provider
requests per candidate, each cut off after ``PROVIDER_CALL_TIMEOUT_S``, and
the whole call stops at ``ENRICHMENT_DEADLINE_S``.

Whenever a provider matched, every catalog field of the
result comes from the provider records (#602), because a signed result may
create shared ``books`` / ``editions`` rows:

- title and author: the Open Library doc (``title``, ``author_name[0]``),
  otherwise the Google Books volume (``volumeInfo.title``, ``authors[0]``);
- edition ISBNs: the candidate's ISBN-13 only when a provider record used
  for the result lists it among its identifiers (Open Library ``isbn``,
  Google Books ``industryIdentifiers``); a lookup by that ISBN returning a
  record is not enough, since Google Books ``q=isbn:X`` is a keyword search.
  The ISBN-10 is kept only when it is the ISBN-10 form of that confirmed
  ISBN-13 (an Open Library work lists every edition's ISBNs, so a listed
  ISBN-10 may belong to another edition); without a confirmed ISBN-13, a
  listed ISBN-10 is kept on its own.

When both providers matched, the Google Books volume is used only if it is
the same book as the Open Library doc (:func:`records_agree`): a shared
ISBN, or the same normalised title and first-author surname. Otherwise it is
dropped entirely (no ``google_books_id``, description, cover or edition
data from it), so one book's Google Books ID is never wired to another
book's work.

The candidate's own title and author are shown only when nothing matched;
such a result has no provider ID, is never signed and so can never create a
catalog row.

The package replaced the single module ``enrichment.py`` (#809); everything
other code imported from it is re-exported here. The modules are: ``text``
(normalisation), ``titles``, ``authors``, ``scoring`` (a record's fit to a
reading), ``isbn`` and ``service`` (``EnrichmentService``).
"""

from app.services.enrichment.authors import reading_surname
from app.services.enrichment.isbn import (
    _edition_isbns,
    isbn_10_to_13,
    isbn_13_to_10,
    normalise_isbn_13,
    valid_isbn_13,
)
from app.services.enrichment.scoring import (
    _gb_parts,
    _ol_parts,
    _scorer,
    reading_match_score,
    records_agree,
)
from app.services.enrichment.service import (
    ENRICHMENT_DEADLINE_S,
    MAX_CONCURRENT_ENRICHMENTS,
    PROVIDER_CALL_TIMEOUT_S,
    TITLE_SEARCH_RESULTS,
    UNKNOWN_AUTHOR,
    EnrichmentService,
)
from app.services.enrichment.text import _normalise, normalise_text, normalise_title
from app.services.enrichment.titles import _is_volume_marker, _split_title, titles_agree

__all__ = [
    "ENRICHMENT_DEADLINE_S",
    "MAX_CONCURRENT_ENRICHMENTS",
    "PROVIDER_CALL_TIMEOUT_S",
    "TITLE_SEARCH_RESULTS",
    "UNKNOWN_AUTHOR",
    "EnrichmentService",
    # Private helpers below are reached by unit tests through the package.
    "_edition_isbns",
    "_gb_parts",
    "_is_volume_marker",
    "_normalise",
    "_ol_parts",
    "_scorer",
    "_split_title",
    "isbn_10_to_13",
    "isbn_13_to_10",
    "normalise_isbn_13",
    "normalise_text",
    "normalise_title",
    "reading_match_score",
    "reading_surname",
    "records_agree",
    "titles_agree",
    "valid_isbn_13",
]
