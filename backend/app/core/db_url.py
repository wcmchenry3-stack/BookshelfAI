"""Database URL normalization shared by the app engine and Alembic."""

import re


def to_async_url(url: str) -> str:
    """Normalize any postgres:// or postgresql:// scheme to postgresql+asyncpg://
    and sqlite:// to sqlite+aiosqlite:// (used by schema-check CI with SQLite)."""
    url = re.sub(r"^postgres(ql)?://", "postgresql+asyncpg://", url)
    return re.sub(r"^sqlite:///", "sqlite+aiosqlite:///", url)
