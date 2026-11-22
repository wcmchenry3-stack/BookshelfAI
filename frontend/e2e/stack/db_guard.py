"""Isolation guard for the web e2e backend (#470).

The e2e launcher writes ``ai_settings`` rows and the fake's
``POST /__forget_scans`` deletes every user's ``scan_records`` (and, by
cascade, ``scan_corrections``). It must therefore never run against a dev,
shared or hosted database. Mirrors ``backend/perf/perf_setup.py`` and reuses
its local-host list.

Allowed: a local host, no query parameters, and a database name containing
``e2e`` (or exactly equal to ``E2E_ALLOW_DB``). Refused: staging or
production ``ENVIRONMENT``, and anything that looks hosted (supabase,
render.com, pooler). Messages never include the URL (it may hold a password).
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

from perf.perf_setup import LOCAL_DB_HOSTS

REQUIRED_DB_NAME_PART = "e2e"
HOSTED_MARKERS = ("supabase", "render.com", "pooler")

# Set once the guard has passed; the fake's destructive hook checks it.
_passed = False


class UnsafeDatabaseError(RuntimeError):
    pass


def check_database_url(
    url: str, environment: str = "development", allow_db: str | None = None
) -> None:
    """Raise :class:`UnsafeDatabaseError` unless ``url`` is a local e2e database."""
    if (environment or "").strip().lower() in {"staging", "production"}:
        raise UnsafeDatabaseError(
            "refusing to run the e2e stack in staging or production"
        )
    lowered = (url or "").lower()
    if any(marker in lowered for marker in HOSTED_MARKERS):
        raise UnsafeDatabaseError("DATABASE_URL looks like a hosted database")
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - any parse failure is a refusal
        raise UnsafeDatabaseError("DATABASE_URL cannot be parsed") from None
    if parsed.host not in LOCAL_DB_HOSTS:
        raise UnsafeDatabaseError("DATABASE_URL must name a local host")
    if parsed.query:
        raise UnsafeDatabaseError(
            "DATABASE_URL must have no query parameters (the driver lets them "
            "override the host and database)"
        )
    name = parsed.database or ""
    if REQUIRED_DB_NAME_PART not in name.lower() and not (
        allow_db and name == allow_db
    ):
        raise UnsafeDatabaseError(
            f"the database name must contain {REQUIRED_DB_NAME_PART!r} (a "
            "throwaway database made for the e2e run), or be named exactly in "
            "E2E_ALLOW_DB"
        )


def check_server_identity(database: str | None, allow_db: str | None = None) -> None:
    """Defence in depth: the database the session really reached."""
    name = database or ""
    if REQUIRED_DB_NAME_PART not in name.lower() and not (
        allow_db and name == allow_db
    ):
        raise UnsafeDatabaseError("the connected database is not an e2e database")


def enforce(url: str, environment: str, allow_db: str | None) -> None:
    """Run the URL checks; on failure exit with the reason, before the app starts."""
    global _passed
    try:
        check_database_url(url, environment, allow_db)
    except UnsafeDatabaseError as exc:
        sys.exit(f"e2e backend refused to start: {exc}")
    _passed = True


def passed() -> bool:
    return _passed
