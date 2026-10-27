import logging
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import TOKEN_REVOKED, get_current_user
from app.auth.google import verify_google_id_token
from app.auth.identity import (
    ACCOUNT_CONFLICT,
    AccountConflictError,
    email_allowed,
    resolve_and_commit_google_user,
)
from app.auth.jwt import (
    access_token_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
    token_version_claim,
)
from app.auth.sessions import revoke_all_sessions
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import (
    GoogleAuthRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
    UserRead,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/google", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
async def google_auth(
    request: Request, body: GoogleAuthRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    logger.info("POST /auth/google — verifying Google ID token")
    try:
        claims = await verify_google_id_token(body.id_token)
        logger.info(
            "Google token verified — email_verified=%s", claims.get("email_verified")
        )
    except Exception:
        logger.exception("Google token verification failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
        )

    email: str = claims.get("email", "")
    if not claims.get("email_verified"):
        logger.warning("Google account email not verified")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified"
        )

    if not email_allowed(email, settings.allowed_emails_list):
        logger.warning("Email not in allowlist")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Email not authorized"
        )

    google_id: str = claims.get("sub") or ""
    if not google_id:
        logger.warning("Google token has no subject")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token"
        )

    # Identity is the Google subject, not the email (#688).
    try:
        user = await resolve_and_commit_google_user(
            db,
            google_id=google_id,
            email=email,
            display_name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )
    except AccountConflictError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ACCOUNT_CONFLICT
        )
    logger.info("Signed-in user resolved (user_id=%s)", user.id)

    access_token = create_access_token(str(user.id), user.email, user.token_version)
    refresh_token, jti = create_refresh_token(str(user.id), user.token_version)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expiry_days)
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=expires_at))
    await db.commit()
    logger.info("Auth successful (user_id=%s)", user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=access_token_ttl_seconds(),
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(settings.rate_limit_auth)
async def refresh(
    request: Request, body: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    try:
        payload = decode_token(body.refresh_token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired"
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type"
        )

    jti = payload.get("jti")
    if not jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )

    result = await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    stored = result.scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token revoked or not found",
        )

    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )

    # A token_version bump revokes the user's refresh rows (sessions.py); the
    # tv claim also catches a refresh token minted by a refresh that raced
    # the bump (#689). Missing tv (issued before #689) is revoked too.
    if token_version_claim(payload) != user.token_version:
        stored.revoked_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_REVOKED
        )

    # Rotate: revoke old token, issue new one
    stored.revoked_at = datetime.now(UTC)
    new_refresh, new_jti = create_refresh_token(str(user.id), user.token_version)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expiry_days)
    db.add(RefreshToken(jti=new_jti, user_id=user.id, expires_at=expires_at))
    access_token = create_access_token(str(user.id), user.email, user.token_version)
    await db.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=access_token_ttl_seconds(),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_auth)
async def logout(
    request: Request, body: LogoutRequest, db: AsyncSession = Depends(get_db)
) -> None:
    try:
        payload = decode_token(body.refresh_token)
    except jwt.InvalidTokenError:
        # Token already invalid — treat as successful logout
        return

    if payload.get("type") != "refresh":
        return

    jti = payload.get("jti")
    if not jti:
        return

    result = await db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
    stored = result.scalar_one_or_none()
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = datetime.now(UTC)
        await db.commit()


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit(settings.rate_limit_auth)
async def logout_all(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Sign out of every device (#689): every access token the user holds
    fails its next request with 401 token_revoked, and no refresh token can
    mint new ones. The caller's own tokens are revoked too."""
    await revoke_all_sessions(db, current_user)
    await db.commit()
    logger.info("All sessions revoked (user_id=%s)", current_user.id)


@router.get("/me", response_model=UserRead)
@limiter.limit(settings.rate_limit_reads)
async def get_me(
    request: Request, current_user: User = Depends(get_current_user)
) -> User:
    return current_user
