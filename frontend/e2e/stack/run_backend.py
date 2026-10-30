"""Start the backend the web e2e suite talks to (#470).

One process: the fake external services (``fake_services.py``, in a thread)
and the real FastAPI app (uvicorn). Nothing here reaches a real model or a
public catalog.

What is different from production, on purpose, and only in this process:

- ``ENVIRONMENT=development`` with ``TEST_AUTH_SECRET`` set, so the dev-only
  ``POST /auth/test-login`` exists (#688). It is not registered when hardened.
- ``AI_ANTHROPIC_BASE_URL`` and ``OPEN_LIBRARY_BASE_URL`` point at the fake
  services, ``ANTHROPIC_API_KEY`` is a dummy, and ``OPENAI_API_KEY`` is empty.
  ``scan.standard`` is switched to the Anthropic adapter in ``ai_settings``
  (the registry default is OpenAI, which has no key here and would answer 503).
- Google Books has no base-URL setting (``GOOGLE_BOOKS_URL`` is a constant in
  ``app/services/google_books.py``), so this launcher rebinds that module
  attribute to the fake before the first request. No app code is changed.
- ``HTTPS_PROXY`` points at a dead local port, so a call that escapes the
  fakes fails instead of reaching the internet.
- ``TURNSTILE_REQUIRED=false``, ``CLAMAV_ENABLED=false``.
- The slowapi limiter is switched off. ``/auth/test-login`` is limited to
  2/minute per IP, and every e2e test signs in through the UI. Limits are
  covered by the backend tests, not here.
- The scan allowance is raised (``quota.free.found_daily``) so repeated local
  runs on one database do not run into it. The suite is not about limits.

Environment: ``DATABASE_URL`` (required, a scratch database, migrated),
``E2E_API_PORT`` (8001), ``E2E_WEB_ORIGIN`` (http://127.0.0.1:8081),
``E2E_FAKE_PORT`` (18080), ``E2E_TEST_AUTH_SECRET``, ``E2E_ALLOW_DB``.

Isolation (``db_guard.py``): it writes ``ai_settings`` and deletes every
``scan_records`` row, so it refuses to start unless ``DATABASE_URL`` is a local
host with no query parameters, the database name contains ``e2e`` (or equals
``E2E_ALLOW_DB``), ``ENVIRONMENT`` is not staging/production and the URL does
not look hosted. The connected database name is checked again before writing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_DIR = HERE.parents[2] / "backend"
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(HERE))

import db_guard

API_PORT = int(os.environ.get("E2E_API_PORT", "8001"))
FAKE_PORT = int(os.environ.get("E2E_FAKE_PORT", "18080"))
WEB_ORIGIN = os.environ.get("E2E_WEB_ORIGIN", "http://127.0.0.1:8081")
FAKE_URL = f"http://127.0.0.1:{FAKE_PORT}"


def configure_environment() -> None:
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL is required (a scratch, migrated database)")
    # Before anything is written (and before ENVIRONMENT is overridden below).
    db_guard.enforce(
        os.environ["DATABASE_URL"],
        os.environ.get("ENVIRONMENT", "development"),
        os.environ.get("E2E_ALLOW_DB"),
    )
    from perf import perf_setup

    private, public = perf_setup.generate_jwt_keys()
    os.environ.update(
        {
            "ENVIRONMENT": "development",
            "JWT_PRIVATE_KEY": private,
            "JWT_PUBLIC_KEY": public,
            "TEST_AUTH_SECRET": os.environ.get("E2E_TEST_AUTH_SECRET", "e2e-secret"),
            "ALLOWED_EMAILS": "",
            "CORS_ORIGINS": f'["{WEB_ORIGIN}"]',
            "TURNSTILE_REQUIRED": "false",
            "TURNSTILE_SECRET_KEY": "",
            "CLAMAV_ENABLED": "false",
            "ANTHROPIC_API_KEY": "e2e-fake-key",
            "AI_ANTHROPIC_BASE_URL": FAKE_URL,
            "OPENAI_API_KEY": "",
            "OPEN_LIBRARY_BASE_URL": FAKE_URL,
            "GOOGLE_BOOKS_API_KEY": "",
            "RATE_LIMIT_SCAN": "100000/minute",
            "SENTRY_DSN": "",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(name, None)


async def seed_ai_settings() -> None:
    from app.ai import settings as ai_settings
    from app.core import database

    try:
        async with database.AsyncSessionLocal() as db:
            from sqlalchemy import func, select

            db_guard.check_server_identity(
                (await db.execute(select(func.current_database()))).scalar_one(),
                os.environ.get("E2E_ALLOW_DB"),
            )
            await ai_settings.switch_provider(
                db, "scan.standard", "anthropic", updated_by="e2e"
            )
            await ai_settings.set_setting(
                db, "quota.free.found_daily", 1000, updated_by="e2e"
            )
            await ai_settings.set_setting(
                db, "scan.requests_per_user_daily", 10000, updated_by="e2e"
            )
    finally:
        await database.engine.dispose()


def forget_scans() -> None:
    """Delete every ``scan_records`` row. Called from the fake services'
    thread, so it uses its own engine and event loop. Only the e2e database."""

    if not db_guard.passed():
        raise RuntimeError("isolation guard has not passed")

    async def run() -> None:
        from sqlalchemy import delete
        from sqlalchemy.ext.asyncio import create_async_engine

        from app.core.config import settings
        from app.models import ScanRecord

        engine = create_async_engine(settings.async_database_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(delete(ScanRecord))
        finally:
            await engine.dispose()

    asyncio.run(run())


def main() -> None:
    import threading

    configure_environment()

    import fake_services

    fakes = fake_services.make_server(
        FAKE_PORT, post_hooks={"/__forget_scans": forget_scans}
    )
    threading.Thread(target=fakes.serve_forever, daemon=True).start()

    asyncio.run(seed_ai_settings())

    import uvicorn

    from app.core.limiter import limiter
    from app.services import google_books

    google_books.GOOGLE_BOOKS_URL = f"{FAKE_URL}/books/v1/volumes"
    limiter.enabled = False
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=API_PORT,
        log_level="warning",
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
