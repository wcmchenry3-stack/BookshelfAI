import logging
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.google import verify_google_id_token
from app.auth.jwt import create_access_token, create_refresh_token, decode_token
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
            "Google token verified — email=%s verified=%s",
            claims.get("email"),
            claims.get("email_verified"),
        )
    except Exception:
        logger.exception("Google token verification failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token",
        )

    email: str = claims.get("email", "")
    if not claims.get("email_verified"):
        logger.warning("Email not verified for %s", email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified"
        )

    if settings.allowed_emails_list and email not in settings.allowed_emails_list:
        logger.warning(
            "Email not in allowlist: %s (allowed: %s)",
            email,
            settings.allowed_emails_list,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Email not authorized"
        )

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        logger.info("Creating new user for %s", email)
        user = User(
            email=email,
            google_id=claims.get("sub", ""),
            display_name=claims.get("name"),
            avatar_url=claims.get("picture"),
        )
        db.add(user)
    else:
        logger.info("Existing user found for %s (id=%s)", email, user.id)
        user.display_name = claims.get("name")
        user.avatar_url = claims.get("picture")

    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(str(user.id), user.email)
    refresh_token, jti = create_refresh_token(str(user.id))
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expiry_days
    )
    db.add(RefreshToken(jti=jti, user_id=user.id, expires_at=expires_at))
    await db.commit()
    logger.info("Auth successful for %s", email)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_expiry_hours * 3600,
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

    # Rotate: revoke old token, issue new one
    stored.revoked_at = datetime.now(timezone.utc)
    new_refresh, new_jti = create_refresh_token(str(user.id))
    expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.refresh_token_expiry_days
    )
    db.add(RefreshToken(jti=new_jti, user_id=user.id, expires_at=expires_at))
    access_token = create_access_token(str(user.id), user.email)
    await db.commit()
    return TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh,
        expires_in=settings.jwt_expiry_hours * 3600,
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
        stored.revoked_at = datetime.now(timezone.utc)
        await db.commit()


@router.get("/me", response_model=UserRead)
@limiter.limit(settings.rate_limit_reads)
async def get_me(
    request: Request, current_user: User = Depends(get_current_user)
) -> User:
    return current_user
