"""Shared harness for the scan limits tests (#584), on real Postgres.

The same patterns the PG integration tests already use
(``tests/integration/test_ai_breaker_db.py``, ``test_enhanced_quota_db.py``,
``test_ai_settings_db.py``), gathered in one place so every limits test drives
the same real code path:

- ``/scan`` is called below the rate limiter (``scan.__wrapped__``) with a
  real session, so ``quota.reserve`` / ``release`` / ``refund``, the cost
  breaker and the usage ledger all run for real;
- the provider is faked at the gateway boundary (the adapter classes
  ``app.ai.gateway.OpenAIVisionProvider`` / ``AnthropicVisionProvider``), so
  no provider client is ever built;
- the request clock (``quota.utc_now``) is patched to a far-past UTC day no
  other test uses, so the day's global count and spend rows are this test's
  own; everything committed is deleted afterwards.
"""

import asyncio
import io
import random
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.datastructures import Headers
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.ai import breaker, entitlements, quota
from app.ai import settings as ai_settings
from app.ai.entitlements import Tier
from app.ai.prices import CURRENT_PRICE_VERSION, cost_micros
from app.ai.providers.base import (
    ProviderConnectError,
    ProviderResult,
    ProviderTimeoutError,
)
from app.ai.registry import get_feature
from app.api import scan as scan_api
from app.core.config import settings
from app.core.database import Base
from app.models.ai_enhanced_zero_result import AIEnhancedZeroResult
from app.models.ai_quota import WINDOW_DAY, WINDOW_MONTH, AIInflight, AIQuota
from app.models.ai_settings import AISetting
from app.models.ai_spend import AISpendDaily
from app.models.ai_usage import AIUsage
from app.models.user import User
from app.schemas.book import EnrichedBook
from app.services.book_identifier import BookCandidate, ScanUnavailableError

SCAN = quota.SCAN_QUOTA_FEATURE
ENHANCED = quota.ENHANCED_QUOTA_FEATURE
DUNE = BookCandidate(title="Dune", author="Frank Herbert", confidence=0.9)
# The billed usage every answering fake reports; priced per model by cost().
USAGE = {"input_tokens": 1_000, "output_tokens": 500, "cache_read_tokens": 2_000}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (10, 120, 30)).save(buf, format="JPEG")
    return buf.getvalue()


_JPEG = _jpeg()


def upload() -> StarletteUploadFile:
    return StarletteUploadFile(
        file=io.BytesIO(_JPEG),
        filename="scan.jpg",
        headers=Headers({"content-type": "image/jpeg"}),
    )


def gateway_feature(enhanced: bool) -> str:
    return "scan.enhanced" if enhanced else "scan.standard"


def estimate(enhanced: bool) -> int:
    """The cost estimate reserved against the breaker for one scan."""
    return get_feature(gateway_feature(enhanced)).est_cost_ceiling_micros


def cost(enhanced: bool, rows: Mapping[str, Any]) -> int:
    """What an answered fake call settles: USAGE priced at the model the
    request resolves under ``rows`` (the ai_settings view)."""
    spec = ai_settings.AISettings(dict(rows)).spec_for(
        get_feature(gateway_feature(enhanced))
    )
    return cost_micros(spec.model, CURRENT_PRICE_VERSION, **USAGE)


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def at(day: date, hh: int = 12, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm, ss), tzinfo=UTC)


class Clock:
    """The request clock (``quota.utc_now``), movable mid-request (for
    example from inside the fake provider, to cross UTC midnight while a
    scan is in flight)."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def patch(self):
        return patch.object(quota, "utc_now", self)


# ---------------------------------------------------------------------------
# The provider, faked at the gateway boundary
# ---------------------------------------------------------------------------


def books(*, outcome: str = "ok") -> ProviderResult:
    return ProviderResult([DUNE], outcome=outcome, **USAGE)


def no_books(*, outcome: str = "ok") -> ProviderResult:
    return ProviderResult([], outcome=outcome, **USAGE)


# What the provider does, by name. Every outcome the gateway and /scan tell
# apart today: an answer (with or without books, and the ledger outcomes
# ok / refused / truncated), a timeout, unavailability (an HTTP error, or the
# connection never made: OpenAI's httpx.ConnectError, Claude's
# ProviderConnectError) and any other error.
OUTCOMES: dict[str, Callable[[], ProviderResult]] = {
    "books": books,
    "no_books": no_books,
    "refused": lambda: no_books(outcome="refused"),
    "truncated": lambda: books(outcome="truncated"),
}
ERRORS: dict[str, Callable[[], BaseException]] = {
    "timeout": lambda: ProviderTimeoutError("slow"),
    "unavailable": lambda: ScanUnavailableError("HTTP 500"),
    "connect_error": lambda: httpx.ConnectError("refused"),
    "claude_connect_error": lambda: ProviderConnectError("unreachable"),
    "error": lambda: RuntimeError("boom"),
}


class FakeProvider:
    """Stands in for the provider adapter classes the gateway builds. Each
    call returns (or raises) the named outcome; ``on_call`` runs first."""

    def __init__(
        self,
        outcome: str = "books",
        *,
        delay: float = 0.0,
        on_call: Callable[[], Any] | None = None,
    ) -> None:
        if outcome not in OUTCOMES and outcome not in ERRORS:
            raise ValueError(f"unknown provider outcome {outcome!r}")
        self.outcome = outcome
        self.delay = delay
        self.on_call = on_call
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.cls = MagicMock(name="VisionProviderClass")
        self.cls.return_value.identify = self.identify

    async def identify(self, _image: bytes) -> ProviderResult:
        self.calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.on_call is not None:
                result = self.on_call()
                if asyncio.iscoroutine(result):
                    await result
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.outcome in ERRORS:
                raise ERRORS[self.outcome]()
            return OUTCOMES[self.outcome]()
        finally:
            self.in_flight -= 1

    @property
    def built(self) -> int:
        """How many provider objects the gateway built."""
        return self.cls.call_count

    @contextmanager
    def patch(self):
        with (
            patch("app.ai.gateway.OpenAIVisionProvider", self.cls),
            patch("app.ai.gateway.AnthropicVisionProvider", self.cls),
        ):
            yield self


# ---------------------------------------------------------------------------
# Tier, settings, downstream
# ---------------------------------------------------------------------------


@contextmanager
def as_tier(tier: Tier):
    """Everyone is Free today; Premium is patched the way the module
    docstring of app/ai/entitlements.py says tests must."""
    with patch.object(entitlements, "tier_for", AsyncMock(return_value=tier)):
        yield


def use_settings(rows: Mapping[str, Any]) -> None:
    """Fill the ai_settings cache as if ``rows`` were just read (the same
    hook every scan test uses)."""
    ai_settings.reset_cache(dict(rows))


@contextmanager
def downstream():
    """Enrichment returns one book per candidate; dedup passes them through;
    Turnstile off."""

    async def enrich(candidates, limit):
        return [
            EnrichedBook(title=c.title, author=c.author, confidence=c.confidence)
            for c in candidates
        ]

    with (
        patch("app.api.scan.EnrichmentService") as enrich_cls,
        patch("app.api.scan.DeduplicationService") as dedup_cls,
        patch.object(settings, "turnstile_required", False),
    ):
        enrich_cls.return_value.enrich = AsyncMock(side_effect=enrich)
        dedup_cls.return_value.check = AsyncMock(side_effect=lambda _d, _u, b: b)
        yield


# ---------------------------------------------------------------------------
# Calling /scan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanOutcome:
    """What the client would see: the HTTP status, the ``detail`` code (None
    for 200), the headers and the 402 body."""

    status: int
    code: str | None
    headers: Mapping[str, str] | None = None
    body: Mapping[str, Any] | None = None
    response: Any = None


async def call_scan(factory, user_id: uuid.UUID, *, enhanced: bool) -> ScanOutcome:
    async with factory() as db:
        user = await db.get(User, user_id)
        try:
            response = await scan_api.scan.__wrapped__(
                request=MagicMock(),
                file=upload(),
                current_user=user,
                db=db,
                cf_turnstile_response=None,
                enhanced=enhanced,
            )
        except scan_api.ScanLimitReachedError as exc:
            return ScanOutcome(exc.status_code, exc.detail, exc.headers, exc.body())
        except HTTPException as exc:
            return ScanOutcome(exc.status_code, exc.detail, exc.headers)
        except Exception as exc:  # noqa: BLE001 - what the app turns into a 500
            return ScanOutcome(500, type(exc).__name__)
    return ScanOutcome(200, None, response=response)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counters:
    daily: int
    monthly: int
    enhanced: int
    global_: int
    zero_results: int

    def delta(self, before: "Counters") -> dict[str, int]:
        return {
            "daily": self.daily - before.daily,
            "monthly": self.monthly - before.monthly,
            "enhanced": self.enhanced - before.enhanced,
            "global": self.global_ - before.global_,
            "zero_results": self.zero_results - before.zero_results,
        }


class LimitsDb:
    """Users, far-past days and seeded rows for one test; all deleted
    afterwards (quota, lease and zero-result rows go with the user via ON
    DELETE CASCADE)."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.users: list[uuid.UUID] = []
        self.days: list[date] = []

    async def make_user(self) -> uuid.UUID:
        uid = uuid.uuid4()
        async with self.factory() as db:
            db.add(User(id=uid, email=f"{uid}@example.com", google_id=f"g-{uid}"))
            await db.commit()
        self.users.append(uid)
        return uid

    def make_day(self) -> date:
        """A far-past UTC day no other test uses (not the 1st or the last of
        a month, so the day and month windows don't share an edge)."""
        while True:
            day = date(1900, 1, 1) + timedelta(days=random.randrange(18_000))
            if 2 <= day.day <= 27 and day not in self.days:
                return self.claim_day(day)

    def claim_day(self, day: date) -> date:
        """Use ``day`` (and the day after it) in this test; its spend rows
        are deleted afterwards."""
        for d in (day, day + timedelta(days=1)):
            if d not in self.days:
                self.days.append(d)
        return day

    async def cleanup(self) -> None:
        async with self.factory() as db:
            await db.execute(
                delete(AISpendDaily).where(AISpendDaily.day.in_(self.days))
            )
            await db.execute(delete(AIUsage).where(AIUsage.user_id.in_(self.users)))
            await db.execute(delete(User).where(User.id.in_(self.users)))
            await db.commit()

    # -- seeding -----------------------------------------------------------

    async def seed_units(
        self, user_id, *, feature: str, kind: str, start: date, units: int
    ) -> None:
        if units <= 0:
            return
        async with self.factory() as db:
            db.add(
                AIQuota(
                    user_id=user_id,
                    feature=feature,
                    window_kind=kind,
                    window_start=start,
                    units_used=units,
                )
            )
            await db.commit()

    async def seed_spend(self, day: date, *, settled: int) -> None:
        async with self.factory() as db:
            await db.execute(delete(AISpendDaily).where(AISpendDaily.day == day))
            db.add(
                AISpendDaily(
                    day=day, reserved_micros=0, settled_micros=settled, alerted_mask=0
                )
            )
            await db.commit()

    async def seed_zero_results(self, user_id, n: int) -> None:
        """``n`` uncharged zero-result enhanced scans inside the window (the
        window runs on the database clock, not the patched request clock)."""
        async with self.factory() as db:
            for _ in range(n):
                db.add(
                    AIEnhancedZeroResult(
                        user_id=user_id, created_at=func.clock_timestamp()
                    )
                )
            await db.commit()

    async def hold_lease(self, user_id) -> uuid.UUID:
        """Another scan of this user is in flight."""
        async with self.factory() as db:
            lease_id = await quota.acquire_lease(db, user_id, 600)
        assert lease_id is not None
        return lease_id

    # -- reading -----------------------------------------------------------

    async def units(self, user_id, *, feature: str, kind: str, start: date) -> int:
        async with self.factory() as db:
            return await quota.units_used(db, user_id, feature, kind, start)

    async def row(self, user_id, *, feature: str, kind: str, start: date):
        """The raw counter (None when no row exists)."""
        async with self.factory() as db:
            return (
                await db.execute(
                    select(AIQuota.units_used).where(
                        AIQuota.user_id == user_id,
                        AIQuota.feature == feature,
                        AIQuota.window_kind == kind,
                        AIQuota.window_start == start,
                    )
                )
            ).scalar_one_or_none()

    async def counters(self, user_id, day: date) -> Counters:
        month = quota.month_start(day)
        async with self.factory() as db:
            daily = await quota.units_used(db, user_id, SCAN, WINDOW_DAY, day)
            monthly = await quota.units_used(db, user_id, SCAN, WINDOW_MONTH, month)
            enhanced = await quota.units_used(
                db, user_id, ENHANCED, WINDOW_MONTH, month
            )
            global_ = await quota.global_units_today(db, day)
            zero = (
                await db.execute(
                    select(func.count())
                    .select_from(AIEnhancedZeroResult)
                    .where(AIEnhancedZeroResult.user_id == user_id)
                )
            ).scalar_one()
        return Counters(daily, monthly, enhanced, global_, int(zero))

    async def spend(self, day: date) -> tuple[int, int]:
        """``(reserved, settled)`` micro-dollars for ``day``."""
        async with self.factory() as db:
            reserved, settled, _ = await breaker.totals(db, day)
        return reserved, settled

    async def lease(self, user_id) -> uuid.UUID | None:
        async with self.factory() as db:
            row = await db.get(AIInflight, user_id)
            return None if row is None else row.lease_id

    async def negative_counters(self) -> int:
        """Rows of this test's users below zero (the CHECK forbids them; a
        belt-and-braces read)."""
        async with self.factory() as db:
            return (
                await db.execute(
                    select(func.count())
                    .select_from(AIQuota)
                    .where(AIQuota.user_id.in_(self.users), AIQuota.units_used < 0)
                )
            ).scalar_one()


def make_factory(pool_size: int = 25):
    engine = create_async_engine(
        settings.async_database_url, pool_size=pool_size, max_overflow=10
    )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---------------------------------------------------------------------------
# ai_settings: the real table and the operator CLI
# ---------------------------------------------------------------------------


async def save_and_clear_settings(factory) -> list[tuple[str, Any, str | None]]:
    async with factory() as db:
        saved = [
            (r.key, r.value, r.updated_by)
            for r in (await db.execute(select(AISetting))).scalars().all()
        ]
        await db.execute(delete(AISetting))
        await db.commit()
    return saved


async def restore_settings(factory, saved) -> None:
    async with factory() as db:
        await db.execute(delete(AISetting))
        for key, value, by in saved:
            db.add(AISetting(key=key, value=value, updated_by=by))
        await db.commit()


async def stored_setting(factory, key: str) -> AISetting | None:
    async with factory() as db:
        return await db.get(AISetting, key)


@dataclass(frozen=True)
class CliResult:
    code: int
    out: str
    err: str


async def cli(*argv: str) -> CliResult:
    """Run ``python -m app.ai.settings <argv>`` in-process, as an operator
    would: ``main()`` runs its own event loop, so it runs in a worker thread
    with its own engine (asyncpg connections belong to one loop)."""

    def run() -> CliResult:
        engine = create_async_engine(settings.async_database_url)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async def dispose() -> None:
            await engine.dispose()

        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(ai_settings, "_open_session", maker),
            patch.object(ai_settings, "_dispose_engine", dispose),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = ai_settings.main(list(argv))
        return CliResult(code, out.getvalue(), err.getvalue())

    return await asyncio.to_thread(run)


class SettingsClock:
    """The ai_settings cache clock: ``expire()`` moves it past the cache TTL,
    as time passing in a running process would (no restart, no reset)."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now

    def expire(self) -> None:
        self.now += settings.ai_settings_cache_ttl_seconds + 1

    def patch(self):
        return patch.object(ai_settings, "_clock", self)
