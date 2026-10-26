import logging

import httpx
from authlib.jose import jwt as authlib_jwt

from app.core.config import settings

logger = logging.getLogger(__name__)
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"


async def verify_google_id_token(id_token: str) -> dict:
    """Verify a Google ID token against Google's JWKS and return its claims."""
    logger.info("Fetching Google JWKS from %s", GOOGLE_CERTS_URL)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        resp = await client.get(GOOGLE_CERTS_URL)
        resp.raise_for_status()
        jwks = resp.json()
    logger.info("JWKS fetched — %d keys", len(jwks.get("keys", [])))

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
