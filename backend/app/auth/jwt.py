import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import settings


def create_access_token(user_id: str, email: str) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expiry_hours),
        "type": "access",
    }
    return jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")


def create_refresh_token(user_id: str) -> tuple[str, str]:
    """Return (encoded_jwt, jti). The jti must be stored in the DB for revocation."""
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    payload = {
        "sub": user_id,
        "iat": now,
        "exp": now + timedelta(days=settings.refresh_token_expiry_days),
        "type": "refresh",
        "jti": jti,
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm="RS256")
    return token, jti


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt.InvalidTokenError subclasses on failure."""
    return jwt.decode(token, settings.jwt_public_key, algorithms=["RS256"])
