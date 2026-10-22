"""Per-user scan result cache by normalised-image SHA-256 (#534).

A user who uploads a photo the service has already analysed for them gets
the stored result back: no provider call, no allowance used (decision #545).

Key
---
:func:`cache_key` is the SHA-256 hex of the user id, the SHA-256 of the
normalised JPEG (the bytes the provider would get, ``app.ai.images``), the
gateway feature, the per-request resolved model and the request fingerprint
(stored as ``prompt_version``), joined with a separator that none of them can
contain. The user id is always part of it: there is no shared or cross-user
scope, and :func:`lookup` also filters on the user. A change of provider or
model (``ai_settings``), of the prompt as rendered, the output schema, the
effort, ``max_tokens``, ``SCAN_MAX_BOOKS`` or an adapter's ``PROMPT_VERSION``
misses.

Every input is decided by the server: the user from the session, the hash
from the bytes after normalisation, the feature from the registry, the model
from ``gateway.resolve_spec`` and the fingerprint from
``gateway.request_fingerprint``. Nothing in the request names a key.

What is stored
--------------
Only results whose provider outcome is ``ok`` with at least one candidate
(the caller decides; an empty ``ok`` list may come from a missing or
unparseable reply, so it is never cached); never a refusal, a truncated
reply or any error. At most ``SCAN_MAX_BOOKS`` candidates. ``result`` is the validated
candidate list (title, author, confidence, ISBN-13, ISBN-10): never the raw
model output and never image bytes. A row read back is validated again
before it is served; one that does not validate is treated as a miss.

Lifetime
--------
``expires_at = created_at + cache.ttl_days`` (default 30). An expired row is
never served (:func:`lookup` filters on ``expires_at``) and is deleted by the
daily cleanup task in bounded batches (:func:`purge_expired`). Each user
keeps at most ``cache.max_rows_per_user`` rows; :func:`store` evicts that
user's oldest rows beyond the cap (bounded per call).

Idempotence: :func:`store` is an upsert on ``cache_key`` followed by an
eviction by rank, so running it again (a commit whose acknowledgement was
lost, then a retry) leaves the same rows.

Callers (``/scan``) must treat any exception here as a miss or a skipped
store: the cache never fails a scan. This module never calls a provider and
must not import ``app.ai.providers``.
"""

import hashlib
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ai_result_cache import AIResultCache
from app.services.book_identifier import BookCandidate

# ASCII unit separator: not in a UUID, a hex hash, a feature name, a model ID
# or a prompt version, so two different inputs cannot join to the same text.
_SEP = "\x1f"
# Rows evicted per store at most; a lowered cap converges over later stores.
EVICT_BATCH_SIZE = 1_000
# Same bounded-batch pattern as the other purges.
PURGE_BATCH_SIZE = 5_000
PURGE_MAX_BATCHES = 20
# What a cached candidate may hold, and how long its text fields may be.
_FIELDS = ("title", "author", "confidence", "isbn_13", "isbn_10")
MAX_TEXT_LENGTH = 500


def max_candidates() -> int:
    """The most candidates a cached result may hold: the scan's own cap
    (``SCAN_MAX_BOOKS``), on write and on read."""
    return settings.scan_max_books


class CacheEntryError(ValueError):
    """A stored row does not hold a valid candidate list."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def image_sha256(image_bytes: bytes) -> str:
    """SHA-256 hex of the normalised image bytes."""
    return hashlib.sha256(image_bytes).hexdigest()


def cache_key(
    *,
    user_id: uuid.UUID,
    image_hash: str,
    feature: str,
    model: str,
    prompt_version: str,
) -> str:
    """``sha256(user_id || image_sha256 || feature || model ||
    prompt_version)`` as lowercase hex."""
    parts = (str(user_id), image_hash, feature, model, prompt_version)
    if any(_SEP in p for p in parts):
        raise ValueError("cache key part contains the separator")
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()


def expires_at(created_at: datetime, ttl_days: int) -> datetime:
    return created_at + timedelta(days=ttl_days)


def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > MAX_TEXT_LENGTH:
        raise CacheEntryError(f"bad {field}")
    return value


def _candidate(item: Any) -> BookCandidate:
    if not isinstance(item, dict) or set(item) - set(_FIELDS):
        raise CacheEntryError("bad candidate")
    confidence = item.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not math.isfinite(confidence)
        or not 0.0 <= confidence <= 1.0
    ):
        raise CacheEntryError("bad confidence")
    return BookCandidate(
        title=_text(item.get("title"), "title"),
        author=_text(item.get("author"), "author"),
        confidence=float(confidence),
        isbn_13=_text(item.get("isbn_13"), "isbn_13", optional=True),
        isbn_10=_text(item.get("isbn_10"), "isbn_10", optional=True),
    )


def serialise(candidates: list[BookCandidate]) -> list[dict[str, Any]]:
    """The validated candidates as JSON: exactly the candidate fields."""
    items = [
        {
            "title": c.title,
            "author": c.author,
            "confidence": c.confidence,
            "isbn_13": c.isbn_13,
            "isbn_10": c.isbn_10,
        }
        for c in candidates
    ]
    # Validate on the way in too, so nothing unexpected is ever stored.
    deserialise(items)
    return items


def deserialise(result: Any) -> list[BookCandidate]:
    """A stored ``result`` as candidates; :class:`CacheEntryError` if it is
    not a valid candidate list."""
    if not isinstance(result, list) or len(result) > max_candidates():
        raise CacheEntryError("bad result")
    return [_candidate(item) for item in result]


async def lookup(
    db: AsyncSession,
    *,
    key: str,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> list[BookCandidate] | None:
    """The cached candidates for ``key`` if an unexpired row of ``user_id``
    exists, else None. The user filter is defence in depth: the key already
    contains the user id. A row that does not validate is a miss. Ends its
    read transaction (commit), so no connection is left idle in a
    transaction."""
    query = select(AIResultCache.result).where(
        AIResultCache.cache_key == key,
        AIResultCache.user_id == user_id,
        AIResultCache.expires_at > (now or utc_now()),
    )
    row = (await db.execute(query)).first()
    await db.commit()
    if row is None:
        return None
    try:
        return deserialise(row[0])
    except CacheEntryError:
        return None


async def store(
    db: AsyncSession,
    *,
    key: str,
    user_id: uuid.UUID,
    image_hash: str,
    feature: str,
    model: str,
    prompt_version: str,
    candidates: list[BookCandidate],
    ttl_days: int,
    max_rows_per_user: int,
    now: datetime | None = None,
) -> None:
    """Upsert this result, then evict the user's oldest rows beyond
    ``max_rows_per_user`` (at most :data:`EVICT_BATCH_SIZE`); one commit.
    Idempotent by key."""
    created = now or utc_now()
    values = {
        "cache_key": key,
        "user_id": user_id,
        "image_sha256": image_hash,
        "feature": feature,
        "model": model,
        "prompt_version": prompt_version,
        "result": serialise(candidates),
        "created_at": created,
        "expires_at": expires_at(created, ttl_days),
    }
    stmt = insert(AIResultCache).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[AIResultCache.cache_key],
        set_={
            "result": stmt.excluded.result,
            "created_at": stmt.excluded.created_at,
            "expires_at": stmt.excluded.expires_at,
        },
    )
    beyond_cap = (
        select(AIResultCache.cache_key)
        .where(AIResultCache.user_id == user_id)
        .order_by(AIResultCache.created_at.desc(), AIResultCache.cache_key.desc())
        .offset(max_rows_per_user)
        .limit(EVICT_BATCH_SIZE)
    )
    try:
        await db.execute(stmt)
        await db.execute(
            delete(AIResultCache).where(
                AIResultCache.user_id == user_id,
                AIResultCache.cache_key.in_(beyond_cap),
            )
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


async def purge_expired(db: AsyncSession, now: datetime | None = None) -> int:
    """Delete expired rows in bounded batches; return how many."""
    cutoff = now or utc_now()
    total = 0
    for _ in range(PURGE_MAX_BATCHES):
        batch = (
            select(AIResultCache.cache_key)
            .where(AIResultCache.expires_at <= cutoff)
            .limit(PURGE_BATCH_SIZE)
        )
        result = await db.execute(
            delete(AIResultCache).where(AIResultCache.cache_key.in_(batch))
        )
        await db.commit()
        deleted = result.rowcount or 0
        total += deleted
        if deleted < PURGE_BATCH_SIZE:
            break
    return total
