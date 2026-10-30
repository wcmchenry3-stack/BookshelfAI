"""Integration-test DB guard (#760).

``tests/conftest.py`` auto-skips ``integration`` tests when ``DATABASE_URL`` is
missing or the dummy URL. That is right for the quick ``test-python`` job but
would hide a broken ``test-migrations`` job (a mass skip looks like a pass).
CI sets ``REQUIRE_DB_TESTS=1`` there; with it set, running without a real
database is an error instead of a skip.
"""

import os
from collections.abc import Mapping

DUMMY_DB_URL = "postgresql+asyncpg://test:test@localhost/test"
REQUIRE_ENV = "REQUIRE_DB_TESTS"


def has_real_db(env: Mapping[str, str] = os.environ) -> bool:
    """True if DATABASE_URL points to a real (non-dummy) database."""
    url = env.get("DATABASE_URL", "")
    return bool(url) and url != DUMMY_DB_URL


def db_required(env: Mapping[str, str] = os.environ) -> bool:
    return env.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes"}


def missing_db_error(env: Mapping[str, str] = os.environ) -> str | None:
    """Error message if the run requires a real DB and has none, else None."""
    if db_required(env) and not has_real_db(env):
        return (
            f"{REQUIRE_ENV}=1 but DATABASE_URL is not a real database; refusing "
            "to skip integration tests silently."
        )
    return None
