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
    LEGACY_TOKEN_VERSION,
    access_token_ttl_seconds,
    create_access_token,
    create_refresh_token,
    decode_token,
    has_token_version_claim,
    token_version_claim,
)
from app.auth.refresh_family import (
    REFRESH_REUSED,
    REFRESH_REVOKED_OR_NOT_FOUND,
    Replay,
    alert_reuse,
    classify_replay,
    family_of,
    is_displaced,
    is_rotated,
    new_family_row,
    successor_row,
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
    db.add(new_family_row(user.id, jti, expires_at))
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
    # A rotated row (#690: revoked_at and used_at set) goes on to reuse
    # detection below; a row revoked any other way (logout, logout-all) is a
    # plain 401.
    if stored is None or (stored.revoked_at is not None and not is_rotated(stored)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REFRESH_REVOKED_OR_NOT_FOUND,
        )
    # Whether the token had already been rotated before this request: if it
    # is rotated only once we hold the lock, a concurrent request with the
    # same token did it while we waited (Replay.RACE), not a later replay.
    # (Displacement also rotates a row; a displaced token is reuse anyway.)
    seen_rotated = is_rotated(stored)

    # FOR UPDATE on the user row: a concurrent revoke_all_sessions bumps
    # token_version (taking this lock) before it revokes refresh rows, so it
    # either commits first (we read the new token_version and reject below)
    # or waits for this rotation to commit and then revokes the row we add.
    # No unrevoked refresh row survives a logout-all (#689).
    user = await db.get(
        User,
        uuid.UUID(payload["sub"]),
        with_for_update=True,
        populate_existing=True,
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found"
        )

    # Re-read the refresh row now that we hold the user lock: a concurrent
    # refresh with the same token may have rotated it while we waited, and
    # the check above saw the pre-lock state. Lock order is user -> refresh
    # rows, the same as revoke_all_sessions.
    result = await db.execute(
        select(RefreshToken)
        .where(RefreshToken.jti == jti)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stored = result.scalar_one_or_none()
    if stored is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REFRESH_REVOKED_OR_NOT_FOUND,
        )
    if is_rotated(stored):
        return await _refresh_rotated_token(db, user, stored, payload, seen_rotated)
    if stored.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REFRESH_REVOKED_OR_NOT_FOUND,
        )

    # A token_version bump revokes the user's refresh rows (sessions.py); the
    # tv claim also catches a refresh token minted by a refresh that raced
    # the bump (#689). A token without tv was issued before #689 and is read
    # as tv=0, so it works only while the user has never bumped (#725); it
    # has passed the same jti, revoked_at and expiry checks above. A present
    # but malformed tv is rejected.
    # TODO(#735): remove the legacy branch after 2026-12-15 (re-anchor the
    # date to the actual production release; see app/auth/jwt.py).
    legacy = not has_token_version_claim(payload)
    token_version = LEGACY_TOKEN_VERSION if legacy else token_version_claim(payload)
    if token_version != user.token_version:
        logger.warning(
            "Refresh rejected: token_version stale (user_id=%s)",
            user.id,
        )
        stored.revoked_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_REVOKED
        )

    if legacy:
        logger.info("refresh accepted legacy token without tv (user_id=%s)", user.id)

    # Rotate: mark the old row rotated and issue its successor (same family;
    # a legacy row has no family and is its own, so its successor gets one).
    # A legacy token (#725) is revoked without ``used_at``: it counts as
    # plainly revoked, like rotations made before migration 0022, so a
    # replay is the plain 401 (no grace retry, no reuse detection) and the
    # tv=0 reading stays confined to this one check. The legacy path ends
    # with #735.
    now = datetime.now(UTC)
    stored.revoked_at = now
    if not legacy:
        stored.used_at = now
    tokens, successor = await _issue_successor(db, user, stored)
    stored.replaced_by_id = successor.id
    await db.commit()
    return tokens


async def _issue_successor(
    db: AsyncSession, user: User, previous: RefreshToken
) -> tuple[TokenResponse, RefreshToken]:
    """Mint an access token and a refresh token in ``previous``'s family, and
    add (and flush) the new refresh row, so rows can point at its id."""
    new_refresh, new_jti = create_refresh_token(str(user.id), user.token_version)
    expires_at = datetime.now(UTC) + timedelta(days=settings.refresh_token_expiry_days)
    row = successor_row(previous, new_jti, expires_at)
    db.add(row)
    await db.flush()
    tokens = TokenResponse(
        access_token=create_access_token(str(user.id), user.email, user.token_version),
        refresh_token=new_refresh,
        expires_in=access_token_ttl_seconds(),
    )
    return tokens, row


async def _refresh_rotated_token(
    db: AsyncSession,
    user: User,
    stored: RefreshToken,
    payload: dict,
    seen_rotated: bool,
) -> TokenResponse:
    """A token that /auth/refresh already rotated is presented again (#690).
    Called with the user row and ``stored`` locked. See
    app/auth/refresh_family.py for the four cases."""
    if token_version_claim(payload) != user.token_version:
        # Every session was already revoked (logout-all or an earlier reuse);
        # nothing left to protect, so no second alert.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=TOKEN_REVOKED
        )

    # Lock order: user, the presented row, then its successor (as the
    # rotation itself). A displaced token is reuse whatever its successor.
    successor = None
    if not is_displaced(stored) and stored.replaced_by_id is not None:
        successor = await db.get(
            RefreshToken,
            stored.replaced_by_id,
            with_for_update=True,
            populate_existing=True,
        )
    now = datetime.now(UTC)
    replay = classify_replay(
        seen_rotated=seen_rotated,
        displaced=is_displaced(stored),
        used_at=stored.used_at,
        successor=successor,
        now=now,
        grace_seconds=settings.refresh_reuse_grace_seconds,
    )

    if replay in (Replay.RETRY, Replay.RACE):
        # RETRY: the client never got the successor. RACE: a concurrent
        # request with this token got it, and it may be a thief's. Either
        # way, issue a fresh pair in the same family and displace the
        # successor: whoever presents it later trips REUSE. ``stored`` keeps
        # pointing at the displaced row, so presenting ``stored`` again is
        # REUSE too (one retry per rotated token).
        tokens, replacement = await _issue_successor(db, user, stored)
        successor.revoked_at = now
        successor.used_at = now
        successor.displaced_at = now
        successor.replaced_by_id = replacement.id
        await db.commit()
        logger.info(
            "Refresh %s; successor displaced (user_id=%s)",
            (
                "retried within the grace window"
                if replay is Replay.RETRY
                else "raced a concurrent refresh"
            ),
            user.id,
        )
        return tokens

    if replay is Replay.ENDED:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REFRESH_REVOKED_OR_NOT_FOUND,
        )

    # Replay.REUSE: someone else holds a copy of this token. Revoke every
    # session the user has (the family's rows and, through the token_version
    # bump, every access token and every other family's refresh tokens).
    family_id = family_of(stored)
    await revoke_all_sessions(db, user)
    await db.commit()
    alert_reuse(user.id, family_id)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REFRESH_REUSED)


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
