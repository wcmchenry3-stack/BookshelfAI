import base64
import json
import logging

import httpx  # noqa: F401  (kept so tests can patch app.auth.google.httpx)
from authlib.jose import jwt as authlib_jwt

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


async def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token against Google's JWKS and return its claims."""
    jwks = await google_jwks_cache.get_jwks(_token_kid(id_token))

    allowed_auds = settings.google_client_ids
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
    claims.validate()
    logger.info("Claims valid")
    return dict(claims)
