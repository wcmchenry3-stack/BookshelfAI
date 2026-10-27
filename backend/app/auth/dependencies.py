import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import decode_token, has_token_version_claim, token_version_claim
from app.core.database import get_db
from app.core.sentry_context import set_sentry_user
from app.models.user import User

_bearer = HTTPBearer()

# 401 detail for an access or refresh token revoked by a token_version bump
# (#689). The client signs out on it instead of retrying.
TOKEN_REVOKED = "token_revoked"

# 401 detail for an access token issued before #689 (no tv claim). Not
# token_revoked, so the client refreshes (the legacy refresh token is still
# accepted while token_version is 0) instead of signing out (#725).
# TODO(#725): remove with the legacy path after 2026-12-15.
TOKEN_OUTDATED = "token_outdated"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token"
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type"
        )

    # Revocation (#689): the token's tv must equal users.token_version, read
    # from the row loaded below. No cache and no extra query. A token without
    # tv was issued before #689: a plain 401 so the client refreshes (#725).
    # A present but malformed tv is revoked.
    if not has_token_version_claim(payload):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_OUTDATED
        )
    token_version = token_version_claim(payload)
    if token_version is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_REVOKED
        )

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )
    if user.token_version != token_version:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_REVOKED
        )
    set_sentry_user(user.id)
    return user
