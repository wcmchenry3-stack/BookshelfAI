"""Reading ``ai_settings`` through the process-local cache.

The cache state lives on the package, once per process: ``ai_settings._cache``
(the last rows read and when) and ``ai_settings._clock`` (the time source,
patched in tests). This module reads and writes both through the package.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import settings as ai_settings
from app.ai.settings.errors import AISettingsUnavailableError
from app.ai.settings.view import AISettings
from app.models.ai_settings import AISetting


@dataclass(frozen=True)
class _CacheEntry:
    rows: Mapping[str, Any]
    loaded_at: float


def reset_cache(rows: Mapping[str, Any] | None = None) -> None:
    """Empty the cache, or fill it with ``rows`` as if just read (tests)."""
    ai_settings._cache = (
        None
        if rows is None
        else _CacheEntry(
            MappingProxyType(ai_settings._sanitise_rows(rows)), ai_settings._clock()
        )
    )


def _with_carry_over(result) -> dict[str, Any]:
    """Rows ``(key, value, updated_by)`` as ``{key: value}``, with the #577
    carry-over (:func:`carried_over`) added."""
    values: dict[str, Any] = {}
    actors: dict[str, str | None] = {}
    for key, value, updated_by in result.all():
        values[key] = value
        actors[key] = updated_by
    return {**values, **ai_settings.carried_over(values, actors)}


async def _load_rows(db: AsyncSession) -> dict[str, Any]:
    query = select(AISetting.key, AISetting.value, AISetting.updated_by)
    if (
        not db.in_transaction()
        and not db.identity_map
        and not (db.new or db.dirty or db.deleted)
    ):
        # No transaction open and the session holds nothing: a short
        # transaction of its own, so no connection is left idle in a
        # transaction afterwards. Restricted to an empty session because a
        # failed outermost rollback would expire every loaded object.
        async with db.begin():
            result = await db.execute(query)
            return ai_settings._with_carry_over(result)
    # Otherwise (a transaction is open, or the session holds objects or
    # pending changes): a SAVEPOINT, so a failed read rolls back only itself;
    # the session stays usable and objects it already loaded (the current
    # user) are not expired. When no transaction was open, the session's
    # autobegun transaction stays open until the caller commits or closes.
    async with db.begin_nested():
        result = await db.execute(query)
        return ai_settings._with_carry_over(result)


async def get_ai_settings(db: AsyncSession) -> AISettings:
    """Return the current settings, from the cache when it is fresh.

    Raises :class:`AISettingsUnavailableError` when the table cannot be read
    and nothing has been read before in this process, or the last value read
    is older than ``AI_SETTINGS_MAX_STALE_SECONDS``.
    """
    now = ai_settings._clock()
    entry = ai_settings._cache
    if entry is not None and now - entry.loaded_at < (
        ai_settings.settings.ai_settings_cache_ttl_seconds
    ):
        return AISettings(entry.rows)
    try:
        raw = await ai_settings._load_rows(db)
    except Exception as exc:  # noqa: BLE001 - any read failure is handled alike
        if entry is not None:
            # Measured after the failed read, so a slow failure cannot stretch
            # the stale window past the limit.
            age = ai_settings._clock() - entry.loaded_at
            if age <= ai_settings.settings.ai_settings_max_stale_seconds:
                ai_settings.logger.warning(
                    "ai_settings: read failed (%s); using the value read %.0fs ago",
                    type(exc).__name__,
                    age,
                )
                return AISettings(entry.rows)
            ai_settings.logger.error(
                "ai_settings: read failed (%s) and the cached value is %.0fs old "
                "(max %ds); failing closed",
                type(exc).__name__,
                age,
                ai_settings.settings.ai_settings_max_stale_seconds,
            )
            raise AISettingsUnavailableError("ai_settings unavailable") from None
        ai_settings.logger.error(
            "ai_settings: read failed (%s) and nothing is cached; failing closed",
            type(exc).__name__,
        )
        raise AISettingsUnavailableError("ai_settings unavailable") from None
    rows = MappingProxyType(ai_settings._sanitise_rows(raw))
    ai_settings._cache = _CacheEntry(rows, now)
    return AISettings(rows)
