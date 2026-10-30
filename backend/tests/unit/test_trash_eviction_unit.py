"""Unit tests for the hard-delete helpers in ``app.services.trash`` (#765).

The database is mocked; the real flows run on Postgres in
``tests/integration/test_soft_delete_trash_db.py``. These assert the helper's
returned count and which follow-up statements it issues.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import trash


def _db(*results):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=list(results))
    return db


def _rows(*custom_ids):
    return SimpleNamespace(
        all=lambda: [SimpleNamespace(custom_book_id=c) for c in custom_ids]
    )


@pytest.mark.asyncio
async def test_delete_entries_with_no_ids_issues_no_statement():
    db = _db()
    assert await trash._delete_entries(db, []) == 0
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_delete_entries_also_deletes_custom_books_of_deleted_rows():
    custom = uuid.uuid4()
    db = _db(_rows(custom, None), MagicMock())
    n = await trash._delete_entries(db, [uuid.uuid4(), uuid.uuid4()])
    assert n == 2
    assert db.execute.await_count == 2
    second_sql = str(db.execute.await_args_list[1].args[0])
    assert "user_custom_books" in second_sql


@pytest.mark.asyncio
async def test_delete_entries_skips_custom_delete_when_none_linked():
    db = _db(_rows(None))
    assert await trash._delete_entries(db, [uuid.uuid4()]) == 1
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_evict_over_cap_returns_number_deleted():
    over = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    select_result = SimpleNamespace(scalars=lambda: iter(over))
    db = _db(select_result, _rows(None, None, None))
    assert await trash.evict_over_cap(db, uuid.uuid4()) == 3


@pytest.mark.asyncio
async def test_evict_over_cap_with_nothing_over_cap_returns_zero():
    select_result = SimpleNamespace(scalars=lambda: iter([]))
    db = _db(select_result)
    assert await trash.evict_over_cap(db, uuid.uuid4()) == 0
    assert db.execute.await_count == 1
