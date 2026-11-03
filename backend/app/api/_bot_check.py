"""Bot-protection helpers for POST /scan: Turnstile and device attestation (#753).

Extracted from scan.py to keep that module under the 600-line cap.
All names used by tests are re-exported from scan.py via an import there.
"""

import logging
import uuid
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.attestation import (
    LiveAndroidIntegrityVerifier,
    StubAppleAttestVerifier,
    verify_nonce,
)
from app.core.config import settings
from app.models.device_key import DeviceKey

logger = logging.getLogger(__name__)

TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def _verify_turnstile(token: str) -> bool:
    """Verify a Cloudflare Turnstile token against the siteverify endpoint.

    Returns True if the token is valid, False otherwise.
    A 5-second timeout prevents a Turnstile outage from blocking the endpoint.
    """
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            resp = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": settings.turnstile_secret_key, "response": token},
            )
            resp.raise_for_status()
            return bool(resp.json().get("success"))
    # Any Turnstile failure (network, timeout, malformed response) is treated as
    # verification failure rather than propagating.
    except Exception as exc:  # noqa: BLE001 - any failure = not verified
        logger.warning("Turnstile verification failed: %s", exc)
        return False


async def _check_turnstile(token: str | None) -> None:
    """Cloudflare Turnstile bot check, enabled when TURNSTILE_REQUIRED=true
    (the default). The startup handler already confirmed the secret key is
    present when required. 400 without a token, 403 when it fails."""
    if settings.turnstile_required:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Missing Turnstile token",
            )
        if not await _verify_turnstile(token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Turnstile verification failed",
            )


async def _check_bot_protection(
    *,
    cf_token: str | None,
    attest_platform: str | None,
    attest_key_id: str | None,
    attest_assertion: str | None,
    attest_token: str | None,
    attest_nonce: str | None,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    """Bot protection for /scan (#753): attestation first, Turnstile fallback.

    If the client sends attestation data (nonce + platform), it is verified
    first. A valid assertion / Play Integrity token grants immediate access
    without a Turnstile check.  If attestation is absent or fails, the
    standard Turnstile path runs (TURNSTILE_REQUIRED still applies).
    """
    if attest_nonce and attest_platform:
        verified = await _verify_attestation(
            platform=attest_platform,
            key_id=attest_key_id,
            assertion_b64=attest_assertion,
            integrity_token=attest_token,
            nonce=attest_nonce,
            user_id=user_id,
            db=db,
        )
        if verified:
            return
        if settings.attestation_required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Attestation verification failed",
            )
    await _check_turnstile(cf_token)


async def _verify_attestation(
    *,
    platform: str,
    key_id: str | None,
    assertion_b64: str | None,
    integrity_token: str | None,
    nonce: str,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """Verify a device assertion (#753).  Returns True on success."""
    if not verify_nonce(nonce, user_id, settings.attestation_nonce_secret):
        return False

    if platform == "ios":
        if not key_id or not assertion_b64:
            return False
        row = (
            await db.execute(
                select(DeviceKey).where(
                    DeviceKey.user_id == user_id,
                    DeviceKey.key_id == key_id,
                    DeviceKey.platform == "ios",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        verifier = StubAppleAttestVerifier()
        result = await verifier.verify_assertion(
            key_id=key_id,
            assertion_b64=assertion_b64,
            public_key_cbor=row.public_key_cbor,
            nonce=nonce,
            stored_counter=row.counter,
        )
        if result is None:
            return False
        row.counter = result.counter
        row.last_used_at = datetime.now(UTC)
        await db.commit()
        return True

    if platform == "android":
        if not integrity_token:
            return False
        pkg = settings.attestation_android_package_name
        if not pkg:
            return False
        verifier = LiveAndroidIntegrityVerifier()
        return await verifier.verify_token(
            token=integrity_token,
            nonce=nonce,
            package_name=pkg,
        )

    return False
