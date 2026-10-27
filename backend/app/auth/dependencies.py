import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.jwt import decode_token, token_version_claim
from app.core.database import get_db
from app.core.sentry_context import set_sentry_user
from app.models.user import User

_bearer = HTTPBearer()

# 401 detail for an access or refresh token revoked by a token_version bump
# (#689). The client signs out on it instead of retrying.
TOKEN_REVOKED = "token_revoked"


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
    # tv (issued before #689) is revoked too.
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
