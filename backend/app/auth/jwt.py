import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import settings

# Claim carrying users.token_version at issue time (#689). An access token
# whose tv differs from the user's current token_version is revoked.
TOKEN_VERSION_CLAIM = "tv"

# Legacy-token grace (#725). Tokens issued before #689 have no tv claim. A
# legacy refresh token is read as tv=0 (every user's token_version started at
# 0, migration 0021), so it works only until the user's first logout-all.
# TODO(#735): remove the legacy path after 2026-12-15, once the #689 release
# has been in production for longer than REFRESH_TOKEN_EXPIRY_DAYS (7) plus
# margin; by then every legacy refresh token has expired. The date assumes a
# release date; re-anchor it to the actual production release date.
LEGACY_TOKEN_VERSION = 0


def access_token_ttl_seconds() -> int:
    """Lifetime of an access token in seconds (the ``expires_in`` we return)."""
    return settings.jwt_access_ttl_minutes * 60


def create_access_token(user_id: str, email: str, token_version: int) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_ttl_minutes),
        "type": "access",
        TOKEN_VERSION_CLAIM: token_version,
    }
    return jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")


def create_refresh_token(user_id: str, token_version: int) -> tuple[str, str]:
    """Return (encoded_jwt, jti). The jti must be stored in the DB for revocation.

    The token also carries ``tv``, so a refresh token issued before a
    token_version bump can never mint new tokens, even one created by a
    refresh that raced the bump (#689)."""
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expiry_days),
        "type": "refresh",
        "jti": jti,
        TOKEN_VERSION_CLAIM: token_version,
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")
    return token, jti


def has_token_version_claim(payload: dict) -> bool:
    """Whether the token carries a ``tv`` claim at all (False for tokens
    issued before #689). A present but malformed tv still counts as present."""
    return TOKEN_VERSION_CLAIM in payload


def token_version_claim(payload: dict) -> int | None:
    """The token's ``tv`` claim, or None when it is missing or not an integer."""
    tv = payload.get(TOKEN_VERSION_CLAIM)
    if isinstance(tv, bool) or not isinstance(tv, int):
        return None
    return tv


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt.InvalidTokenError subclasses on failure."""
    return jwt.decode(token, settings.jwt_public_key, algorithms=["RS256"])
