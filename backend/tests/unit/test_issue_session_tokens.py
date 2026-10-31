"""issue_session_tokens: the one place a sign-in mints a token pair (#844)."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from app.auth.jwt import access_token_ttl_seconds, decode_token
from app.auth.sessions import IssuedTokens, issue_session_tokens
from app.core.config import settings
from app.models.refresh_token import RefreshToken
from app.models.user import User
from tests.helpers import install_jwt_keys, rsa_pem_pair

_KEYS = rsa_pem_pair()
_APP = Path(__file__).resolve().parents[2] / "app"


def _user() -> User:
    user = User(id=uuid.uuid4(), email="i@example.com", google_id="g-i")
    user.token_version = 3
    return user


def _db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    return db


async def test_returns_valid_pair_with_matching_claims():
    user = _user()

    with install_jwt_keys(_KEYS):
        tokens = await issue_session_tokens(_db(), user)
        access = decode_token(tokens.access_token)
        refresh = decode_token(tokens.refresh_token)

    assert isinstance(tokens, IssuedTokens)
    assert access["sub"] == refresh["sub"] == str(user.id)
    assert access["email"] == user.email
    assert access["tv"] == refresh["tv"] == 3
    assert access["type"] == "access" and refresh["type"] == "refresh"
    assert access["exp"] - access["iat"] == access_token_ttl_seconds()
    assert refresh["exp"] > access["exp"]


async def test_adds_one_family_row_and_commits_once():
    user, db = _user(), _db()
    before = datetime.now(UTC)

    with install_jwt_keys(_KEYS):
        tokens = await issue_session_tokens(db, user)
        jti = decode_token(tokens.refresh_token)["jti"]

    db.add.assert_called_once()
    row = db.add.call_args.args[0]
    assert isinstance(row, RefreshToken)
    assert row.user_id == user.id
    assert row.jti == jti
    assert row.family_id == row.id
    ttl = timedelta(days=settings.refresh_token_expiry_days)
    assert before + ttl <= row.expires_at <= datetime.now(UTC) + ttl
    db.commit.assert_awaited_once()


async def test_expires_in_is_access_token_ttl():
    with install_jwt_keys(_KEYS):
        tokens = await issue_session_tokens(_db(), _user())

    assert tokens.expires_in == access_token_ttl_seconds()


async def test_commits_after_adding_the_row():
    order = []
    db = _db()
    db.add.side_effect = lambda _row: order.append("add")
    db.commit.side_effect = lambda: order.append("commit")

    with install_jwt_keys(_KEYS):
        await issue_session_tokens(db, _user())

    assert order == ["add", "commit"]


def _function(path: str, name: str) -> ast.AST:
    tree = ast.parse((_APP / path).read_text())
    return next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == name
    )


def _called_names(fn: ast.AST) -> set[str]:
    """Names of every call, bare (``f()``) or attribute (``mod.f()``)."""
    names = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


def test_both_sign_in_routes_use_the_helper_not_inline_issuing():
    """Pins the #844 de-duplication so the 8-line clone cannot come back."""
    for path, name in (
        ("api/auth.py", "google_auth"),
        ("main.py", "test_login"),
    ):
        called = _called_names(_function(path, name))
        assert "issue_session_tokens" in called, name
        assert not called & {
            "create_access_token",
            "create_refresh_token",
            "new_family_row",
            "access_token_ttl_seconds",
            "RefreshToken",
        }, name
