"""Seeding, reading and writing the ``ai_settings`` rows (uncached)."""

import asyncio
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import settings as ai_settings
from app.ai.registry import FeatureSpecError
from app.ai.settings.errors import AISettingValueError
from app.models.ai_settings import AISetting


def seed_values() -> dict[str, Any]:
    """Every key's env/registry default, validated like a write."""
    values: dict[str, Any] = {}
    for key, definition in ai_settings.DEFINITIONS.items():
        values[key] = ai_settings.validate_value(key, definition.default())
    for _feature, message in ai_settings._pair_problems(values):
        raise AISettingValueError(message)
    return values


async def seed_ai_settings(db: AsyncSession) -> int:
    """Insert every missing key; existing rows are left as they are.

    A ``found_daily`` key is seeded with the value carried over from an
    operator's ``quota.<tier>.daily`` row when there is one (#577,
    :func:`carried_over`), written by :data:`SEED_ACTOR` like any seed.

    Returns how many rows were inserted. Idempotent.
    """
    stored = await ai_settings.read_rows(db)
    carried = ai_settings.carried_over(
        {k: r.value for k, r in stored.items()},
        {k: r.updated_by for k, r in stored.items()},
    )
    for key in carried:
        ai_settings.logger.info(
            "ai_settings: %s carried over from the retired daily key", key
        )
    stmt = (
        insert(AISetting)
        .values(
            [
                {"key": key, "value": value, "updated_by": ai_settings.SEED_ACTOR}
                for key, value in {**ai_settings.seed_values(), **carried}.items()
            ]
        )
        .on_conflict_do_nothing(index_elements=[AISetting.key])
        .returning(AISetting.key)
    )
    result = await db.execute(stmt)
    inserted = len(result.scalars().all())
    await db.commit()
    return inserted


def _open_session() -> AsyncSession:
    return ai_settings.database.AsyncSessionLocal()


async def _dispose_engine() -> None:
    await ai_settings.database.engine.dispose()


async def seed_ai_settings_at_startup() -> None:
    """Seed at app startup. Never raises: with the DB unreachable the app
    still starts, and requests fail closed until the table can be read."""
    try:
        async with ai_settings._open_session() as db:
            inserted = await asyncio.wait_for(
                ai_settings.seed_ai_settings(db),
                timeout=ai_settings.SEED_TIMEOUT_SECONDS,
            )
        ai_settings.logger.info("ai_settings: seeded %d missing key(s)", inserted)
    except Exception as exc:  # noqa: BLE001 - startup must not crash here
        ai_settings.logger.warning(
            "ai_settings: seeding skipped (%s); scans fail closed until the "
            "table can be read",
            type(exc).__name__,
        )


async def read_rows(db: AsyncSession) -> dict[str, AISetting]:
    """Every stored row, uncached (CLI)."""
    result = await db.execute(select(AISetting))
    return {row.key: row for row in result.scalars().all()}


def _validate_actor(updated_by: str) -> str:
    updated_by = updated_by.strip()
    if (
        not 0 < len(updated_by) <= ai_settings.MAX_UPDATED_BY_LENGTH
        or not updated_by.isprintable()
    ):
        raise AISettingValueError(
            f"--by must be 1..{ai_settings.MAX_UPDATED_BY_LENGTH} printable characters"
        )
    return updated_by


def _upsert(key: str, typed: Any, updated_by: str):
    stmt = insert(AISetting).values(key=key, value=typed, updated_by=updated_by)
    return stmt.on_conflict_do_update(
        index_elements=[AISetting.key],
        set_={
            "value": stmt.excluded.value,
            "updated_by": stmt.excluded.updated_by,
            "updated_at": func.now(),
        },
    )


async def set_setting(
    db: AsyncSession, key: str, value: Any, *, updated_by: str
) -> Any:
    """Validate and upsert one key. Returns the stored (typed) value.

    Takes effect in each process within the cache TTL.
    """
    updated_by = ai_settings._validate_actor(updated_by)
    ai_settings.validate_value(key, value)
    stored = await ai_settings.read_rows(db)
    current = ai_settings._sanitise_rows({k: row.value for k, row in stored.items()})
    typed = ai_settings.validate_write(key, value, current)
    await db.execute(ai_settings._upsert(key, typed, updated_by))
    await db.commit()
    return typed


def validate_switch(
    feature: str, provider: Any, model: Any | None = None
) -> tuple[str, str]:
    """Validate switching ``feature`` to ``provider`` (and ``model``, else the
    registry's default model for that provider). Returns the typed pair."""
    if feature not in ai_settings.SCAN_FEATURES:
        raise AISettingValueError(
            f"unknown feature {feature!r}; known: {', '.join(ai_settings.SCAN_FEATURES)}"
        )
    provider = ai_settings.validate_value(f"{feature}.provider", provider)
    if model is None:
        try:
            model = ai_settings.default_model(feature, provider)
        except FeatureSpecError as exc:
            raise AISettingValueError(str(exc)) from None
    model = ai_settings.validate_value(f"{feature}.model", model)
    pair = {f"{feature}.provider": provider, f"{feature}.model": model}
    for _feature, message in ai_settings._pair_problems(pair):
        raise AISettingValueError(f"{feature}: {message}")
    return provider, model


async def switch_provider(
    db: AsyncSession,
    feature: str,
    provider: Any,
    model: Any | None = None,
    *,
    updated_by: str,
) -> tuple[str, str]:
    """Write ``<feature>.provider`` and ``<feature>.model`` together (one
    commit), so the pair is never half-switched. Returns the stored pair.

    Takes effect in each process within the cache TTL. The provider's API key
    is not checked here (the CLI may run with a different environment than the
    app); a feature switched to a provider whose key is missing answers 503
    ``scan_unavailable`` (refunded) until the key is set.
    """
    updated_by = ai_settings._validate_actor(updated_by)
    provider, model = ai_settings.validate_switch(feature, provider, model)
    await db.execute(ai_settings._upsert(f"{feature}.provider", provider, updated_by))
    await db.execute(ai_settings._upsert(f"{feature}.model", model, updated_by))
    await db.commit()
    return provider, model
