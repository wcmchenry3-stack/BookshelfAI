"""
Shared slowapi Limiter instance.

Lives in app.core to avoid circular imports — main.py and every router
import from here instead of from each other.
"""

import logging

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

logger = logging.getLogger(__name__)


def _get_user_id_or_ip(request: Request) -> str:
    """Rate-limit key: authenticated user ID when a valid JWT is present, else remote IP."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from app.auth.jwt import decode_token

            payload = decode_token(auth[7:])
            return f"user:{payload['sub']}"
        # Any decode failure falls back to IP-based limiting below.
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Rate-limit key: falling back to IP (token decode failed: %s)", exc
            )
    return get_remote_address(request)


limiter = Limiter(key_func=_get_user_id_or_ip)
