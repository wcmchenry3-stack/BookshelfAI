"""revoke_all_sessions keeps the in-memory user current (#689)."""

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import inspect

from app.auth.sessions import revoke_all_sessions
from app.models.user import User


async def test_sets_token_version_from_returning_without_marking_dirty():
    user = User(id=uuid.uuid4(), email="s@example.com", google_id="g-s")
    user.token_version = 0
    bumped = MagicMock()
    bumped.scalar_one.return_value = 4  # another bump landed first
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[bumped, MagicMock()])

    await revoke_all_sessions(db, user)

    assert user.token_version == 4
    # Set as the committed value: a later flush cannot write it back over a
    # concurrent bump.
    assert not inspect(user).attrs.token_version.history.has_changes()
    users_update, tokens_update = (c.args[0] for c in db.execute.await_args_list)
    assert users_update.table.name == "users"
    assert [c.name for c in users_update._returning] == ["token_version"]
    assert tokens_update.table.name == "refresh_tokens"
    db.commit.assert_not_called()
