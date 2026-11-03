"""Device attestation endpoints (#753).

POST /attestation/nonce  — issue a signed server nonce (both platforms)
POST /attestation/register — register an iOS App Attest key (iOS only)
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.attestation import (
    AttestationResult,
    StubAppleAttestVerifier,
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
    return StubAppleAttestVerifier()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class NonceResponse(BaseModel):
    nonce: str


class RegisterRequest(BaseModel):
    key_id: str
    attestation_b64: str  # base64url-encoded CBOR attestation object
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

    The client calls ``dcAppAttestService.attest(keyId, SHA256(nonce))`` and
    sends the resulting attestation object here. On success the public key and
    initial counter are stored; subsequent /scan calls can use assertions
    instead of Turnstile tokens.
    """
    if not settings.attestation_apple_app_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="attestation_not_configured",
        )
    if not verify_nonce(body.nonce, current_user.id, settings.attestation_nonce_secret):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_nonce",
        )

    result: AttestationResult | None = await apple_verifier.verify_attestation(
        key_id=body.key_id,
        attestation_b64=body.attestation_b64,
        nonce=body.nonce,
        app_id=settings.attestation_apple_app_id,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="attestation_invalid",
        )

    # Upsert: replace a stale key for the same key_id rather than failing.
    existing = await db.execute(
        select(DeviceKey).where(
            DeviceKey.user_id == current_user.id,
            DeviceKey.key_id == body.key_id,
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = DeviceKey(
            id=uuid.uuid4(),
            user_id=current_user.id,
            platform="ios",
            key_id=body.key_id,
            public_key_cbor=result.public_key_cbor,
            counter=result.counter,
        )
        db.add(row)
    else:
        row.public_key_cbor = result.public_key_cbor
        row.counter = result.counter

    await db.commit()
    logger.info("device_key registered user=%s key_id=%.16s", current_user.id, body.key_id)
    return RegisterResponse(registered=True)
