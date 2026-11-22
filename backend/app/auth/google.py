import base64
import hashlib
import json
import logging

import httpx  # noqa: F401 - kept so tests can patch app.auth.google.httpx
from authlib.jose import jwt as authlib_jwt
from authlib.jose.errors import InvalidClaimError

from app.auth.jwks_cache import JwksCache
from app.core.config import settings

logger = logging.getLogger(__name__)
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"

google_jwks_cache = JwksCache(GOOGLE_CERTS_URL)


def _token_kid(id_token: str) -> str | None:
    """Best-effort read of the unverified ``kid`` header; None if unreadable."""
    try:
        header_b64 = id_token.split(".", 1)[0]
        padded = header_b64 + "=" * (-len(header_b64) % 4)
        kid = json.loads(base64.urlsafe_b64decode(padded)).get("kid")
        return kid if isinstance(kid, str) else None
    except (ValueError, TypeError, AttributeError):
        return None


def _fingerprint(value: str) -> str:
    """12-char SHA-256 prefix: lets operators compare a value without logging it."""
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _aud_fingerprints(aud: object) -> str:
    """Fingerprint a token ``aud`` claim (str or list of str, per the JWT spec)."""
    if isinstance(aud, str) and aud:
        return _fingerprint(aud)
    if isinstance(aud, list | tuple):
        parts = [_fingerprint(a) for a in aud if isinstance(a, str) and a]
        if parts:
            return ",".join(parts)
    return "missing"


def _slot_fingerprint(value: str | None) -> str:
    return _fingerprint(value) if value else "unset"


def _log_audience_mismatch(claims: dict) -> None:
    """Warn with fingerprints only (never raw ``aud`` / client IDs, docs/security.md)."""
    logger.warning(
        "Google token audience not allowed: token_aud=%s web=%s ios=%s android=%s",
        _aud_fingerprints(claims.get("aud")),
        _slot_fingerprint(settings.google_client_id),
        _slot_fingerprint(settings.google_ios_client_id),
        _slot_fingerprint(settings.google_android_client_id),
    )


async def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token against Google's JWKS and return its claims."""
    jwks = await google_jwks_cache.get_jwks(_token_kid(id_token))

    allowed_auds = settings.google_client_ids
    if not allowed_auds:
        # Fail closed: authlib skips the aud check when ``values`` is empty,
        # which would accept a Google token issued to any app (#988).
        logger.warning("Google sign-in refused: no Google client IDs configured")
        raise ValueError("no Google client IDs configured")
    logger.info("Decoding token")
    claims = authlib_jwt.decode(
        id_token,
        jwks,
        claims_options={
            "iss": {
                "essential": True,
                "values": ["https://accounts.google.com"],
            },
            "aud": {
                "essential": True,
                "values": allowed_auds,
            },
        },
    )
    logger.info("Token decoded — validating claims")
    try:
        claims.validate()
    except InvalidClaimError as exc:
        if exc.claim_name == "aud":
            _log_audience_mismatch(claims)
        raise
    logger.info("Claims valid")
    return dict(claims)
