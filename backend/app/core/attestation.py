"""Device attestation — nonce generation and verifier interfaces (#753).

Nonces are HMAC-SHA256-signed tokens that bind a challenge to a user ID and
carry a 5-minute expiry.  They are stateless: no database write is needed to
issue one, and verification is a pure HMAC check.

Verifiers are injected via FastAPI dependencies so tests can swap in fakes.
The default implementations call the real Apple / Google APIs; they require
ATTESTATION_NONCE_SECRET (all), plus ATTESTATION_ANDROID_PACKAGE_NAME and
ATTESTATION_GOOGLE_CREDENTIALS_JSON for Android.

Apple App Attest two-step flow:
  1. POST /attestation/nonce → nonce
  2. POST /attestation/register (key_id + attestation_object + nonce)
     → server verifies attestation cert chain, stores public key
  3. POST /scan (key_id + assertion_b64 + nonce) per request
     → server verifies assertion signature and counter, updates counter

Android Play Integrity flow (no persistent key):
  1. POST /attestation/nonce → nonce
  2. Client requests Play Integrity token from Google with that nonce
  3. POST /scan (platform=android + integrity_token + nonce) per request
     → server decodes token via Google API, checks nonce + verdicts
"""

import base64
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_NONCE_TTL_SECONDS = 300  # 5 minutes
_NONCE_SEP = "."


# ---------------------------------------------------------------------------
# HMAC nonce (stateless, shared by iOS and Android)
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def issue_nonce(user_id: uuid.UUID, secret: str) -> str:
    """Return a signed nonce for a device attestation request.

    Format: ``{payload_b64url}.{sig_b64url}``  where payload is the UTF-8
    encoding of ``{user_id}:{utc_seconds}:{random_hex}``.
    """
    ts = int(time.time())
    rand = secrets.token_hex(16)
    payload = f"{user_id}:{ts}:{rand}".encode()
    sig = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64url_encode(payload)}{_NONCE_SEP}{_b64url_encode(sig)}"


def verify_nonce(nonce: str, user_id: uuid.UUID, secret: str) -> bool:
    """Return True if the nonce is valid, unexpired, and belongs to user_id."""
    if not secret:
        return False
    parts = nonce.split(_NONCE_SEP, 1)
    if len(parts) != 2:
        return False
    try:
        payload = _b64url_decode(parts[0])
        sig = _b64url_decode(parts[1])
    except Exception:  # noqa: BLE001 - malformed nonce, treat as invalid
        return False
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        uid_str, ts_str, _ = payload.decode().split(":", 2)
    except ValueError:
        return False
    if uid_str != str(user_id):
        return False
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    return (int(time.time()) - ts) <= _NONCE_TTL_SECONDS


# ---------------------------------------------------------------------------
# Apple App Attest verifier (iOS)
# ---------------------------------------------------------------------------


@dataclass
class AttestationResult:
    """Parsed and verified attestation data, ready to store as a DeviceKey."""

    public_key_cbor: bytes
    counter: int


@dataclass
class AssertionResult:
    """Verified assertion — carries the new (post-assertion) counter."""

    counter: int


@runtime_checkable
class AppleAttestVerifier(Protocol):
    async def verify_attestation(
        self,
        key_id: str,
        attestation_b64: str,
        nonce: str,
        app_id: str,
    ) -> AttestationResult | None:
        """Verify an App Attest attestation object.

        Returns AttestationResult (public key + counter) on success, None on
        any failure. ``nonce`` is the raw server nonce string; the verifier
        computes SHA-256(nonce) to reconstruct the client data hash embedded
        in the attestation. ``app_id`` is ``<TEAM_ID>.<BUNDLE_ID>``.
        """
        ...

    async def verify_assertion(
        self,
        key_id: str,
        assertion_b64: str,
        public_key_cbor: bytes,
        nonce: str,
        stored_counter: int,
    ) -> AssertionResult | None:
        """Verify an App Attest assertion for one /scan request.

        Returns AssertionResult (new counter) on success, None on any failure.
        The verifier hashes ``nonce`` as the client data and checks the
        signature against ``public_key_cbor``. Rejects if the assertion
        counter is not strictly greater than ``stored_counter``.
        """
        ...


class StubAppleAttestVerifier:
    """Always-reject stub — used when ATTESTATION_APPLE_APP_ID is not set."""

    async def verify_attestation(self, *_args, **_kwargs) -> AttestationResult | None:
        logger.warning("Apple App Attest not configured — attestation rejected")
        return None

    async def verify_assertion(self, *_args, **_kwargs) -> AssertionResult | None:
        logger.warning("Apple App Attest not configured — assertion rejected")
        return None


# ---------------------------------------------------------------------------
# Android Play Integrity verifier
# ---------------------------------------------------------------------------


@runtime_checkable
class AndroidIntegrityVerifier(Protocol):
    async def verify_token(
        self,
        token: str,
        nonce: str,
        package_name: str,
    ) -> bool:
        """Decode a Play Integrity token via Google's API.

        Returns True only when the token is valid, the nonce matches, and
        both MEETS_DEVICE_INTEGRITY and PLAY_RECOGNIZED are present.
        """
        ...


class StubAndroidIntegrityVerifier:
    """Always-reject stub — used when Play Integrity is not configured."""

    async def verify_token(self, *_args, **_kwargs) -> bool:
        logger.warning("Android Play Integrity not configured — token rejected")
        return False


class LiveAndroidIntegrityVerifier:
    """Calls the Play Integrity decodeIntegrityToken API.

    Requires ATTESTATION_GOOGLE_CREDENTIALS_JSON (service-account JSON) and
    ATTESTATION_ANDROID_PACKAGE_NAME in the environment.
    """

    _DECODE_URL = (
        "https://playintegrity.googleapis.com/v1/{package_name}:decodeIntegrityToken"
    )
    _REQUIRED_DEVICE_VERDICTS = {"MEETS_DEVICE_INTEGRITY"}  # noqa: RUF012
    _REQUIRED_APP_VERDICT = "PLAY_RECOGNIZED"

    async def verify_token(self, token: str, nonce: str, package_name: str) -> bool:
        import httpx

        from app.core.config import settings

        creds_json = settings.attestation_google_credentials_json
        if not creds_json:
            logger.warning("ATTESTATION_GOOGLE_CREDENTIALS_JSON not set")
            return False
        try:
            access_token = await _google_access_token(creds_json)
            url = self._DECODE_URL.format(package_name=package_name)
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    json={"integrity_token": token},
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                resp.raise_for_status()
            outer = resp.json().get("tokenPayloadExternal", {})
        except (
            Exception  # noqa: BLE001 - any API/network failure = not verified
        ) as exc:
            logger.warning("Play Integrity API call failed: %s", exc)
            return False

        req = outer.get("requestDetails", {})
        if req.get("nonce") != nonce:
            return False
        if req.get("requestPackageName") != package_name:
            return False

        device_verdicts = set(
            outer.get("deviceIntegrity", {}).get("deviceRecognitionVerdict", [])
        )
        if not (device_verdicts & self._REQUIRED_DEVICE_VERDICTS):
            return False

        app_verdict = outer.get("appIntegrity", {}).get("appRecognitionVerdict", "")
        return app_verdict == self._REQUIRED_APP_VERDICT


async def _google_access_token(service_account_json: str) -> str:
    """Exchange a service-account JSON string for a short-lived access token."""
    import json
    import time as _time

    import httpx
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    sa = json.loads(service_account_json)
    now = int(_time.time())
    header_b64 = _b64url_encode(b'{"alg":"RS256","typ":"JWT"}')
    claim_b64 = _b64url_encode(
        json.dumps(
            {
                "iss": sa["client_email"],
                "scope": "https://www.googleapis.com/auth/playintegrity",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": now,
                "exp": now + 3600,
            }
        ).encode()
    )
    msg = f"{header_b64}.{claim_b64}".encode()
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig_b64 = _b64url_encode(key.sign(msg, padding.PKCS1v15(), hashes.SHA256()))
    jwt = f"{header_b64}.{claim_b64}.{sig_b64}"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt,
            },
        )
        resp.raise_for_status()
    return resp.json()["access_token"]
