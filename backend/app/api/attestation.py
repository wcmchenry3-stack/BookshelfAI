"""Device attestation endpoints (#753).

POST /attestation/nonce  — issue a signed server nonce (both platforms)
POST /attestation/register — register an iOS App Attest key (iOS only)
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.app_attest import AppAttestError, apple_verifier, canonical_key_id
from app.core.attestation import (
    AttestationResult,
    issue_nonce,
    verify_nonce,
)
from app.core.config import settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.models.device_key import DeviceKey
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attestation", tags=["attestation"])


# ---------------------------------------------------------------------------
# Dependency — Apple verifier (swapped in tests)
# ---------------------------------------------------------------------------


def get_apple_verifier():
    return apple_verifier()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class NonceResponse(BaseModel):
    nonce: str


class RegisterRequest(BaseModel):
    key_id: str
    # CBOR attestation object, standard base64 as DCAppAttestService returns
    # it (base64url is accepted too).
    attestation_b64: str
    nonce: str


class RegisterResponse(BaseModel):
    registered: bool


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/nonce", response_model=NonceResponse)
@limiter.limit(settings.rate_limit_reads)
async def get_nonce(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> NonceResponse:
    """Issue a fresh HMAC-signed nonce for one attestation or assertion."""
    if not settings.attestation_nonce_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="attestation_not_configured",
        )
    return NonceResponse(
        nonce=issue_nonce(current_user.id, settings.attestation_nonce_secret)
    )


@router.post("/register", response_model=RegisterResponse)
@limiter.limit(settings.rate_limit_writes)
async def register_device(
    request: Request,
    body: RegisterRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    apple_verifier=Depends(get_apple_verifier),
) -> RegisterResponse:
    """Register an iOS App Attest key for the authenticated user.

    The client calls ``DCAppAttestService.attestKey(keyId, SHA256(nonce))`` and
    sends the resulting attestation object here. On success the public key and
    initial counter are stored; subsequent /scan calls can use assertions
    instead of Turnstile tokens.
    """
    if not settings.attestation_apple_app_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="attestation_not_configured",
        )
    # Read once, before any commit or rollback: get_current_user shares this
    # session, and a rollback expires current_user, so touching it afterwards
    # would lazy-load on an async session (MissingGreenlet, a 500).
    user_id = current_user.id
    if not verify_nonce(body.nonce, user_id, settings.attestation_nonce_secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_nonce",
        )

    try:
        key_id = canonical_key_id(body.key_id)
    except AppAttestError:
        raise _attestation_invalid() from None

    result: AttestationResult | None = await apple_verifier.verify_attestation(
        key_id=key_id,
        attestation_b64=body.attestation_b64,
        nonce=body.nonce,
        app_id=settings.attestation_apple_app_id,
    )
    if result is None:
        raise _attestation_invalid()

    # A registered key is never overwritten: its counter is what makes an
    # assertion single-use, and nonces are reusable for 5 minutes, so an
    # overwrite (counter back to 0) would re-open every old assertion. The
    # same key again is a client retry after a lost response: same answer,
    # row unchanged. A different key under this key_id is refused.
    existing = await _ios_key(db, user_id, key_id)
    if existing is None:
        db.add(
            DeviceKey(
                id=uuid.uuid4(),
                user_id=user_id,
                platform="ios",
                key_id=key_id,
                public_key_cbor=result.public_key_cbor,
                counter=result.counter,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # A concurrent registration of the same key_id won the insert.
            await db.rollback()
            existing = await _ios_key(db, user_id, key_id)
        else:
            logger.info("device_key registered user=%s key_id=%.16s", user_id, key_id)
            return RegisterResponse(registered=True)
    if existing is None or existing.public_key_cbor != result.public_key_cbor:
        raise _attestation_invalid()
    return RegisterResponse(registered=True)


def _attestation_invalid() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="attestation_invalid",
    )


async def _ios_key(
    db: AsyncSession, user_id: uuid.UUID, key_id: str
) -> DeviceKey | None:
    return (
        await db.execute(
            select(DeviceKey).where(
                DeviceKey.user_id == user_id,
                DeviceKey.key_id == key_id,
                DeviceKey.platform == "ios",
            )
        )
    ).scalar_one_or_none()
