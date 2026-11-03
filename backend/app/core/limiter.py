"""
Shared slowapi Limiter instance.

Lives in app.core to avoid circular imports — main.py and every router
import from here instead of from each other.
"""

import logging

from fastapi import Request
from slowapi import Limiter

from app.core.client_ip import client_ip_bucket

logger = logging.getLogger(__name__)


def _get_user_id_or_ip(request: Request) -> str:
    """Rate-limit key: ``user:<sub>`` for a valid access token, else the client IP.

    Only a verified token with ``type == "access"`` counts. A refresh token
    (or any other token type) sent as a bearer falls back to the IP key, so it
    can't be used to move a caller into a per-user bucket. The IP key comes
    from the single resolver in ``app.core.client_ip`` (IPv6 grouped by /64).
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from app.auth.jwt import decode_token

            payload = decode_token(auth[7:])
            sub = payload.get("sub")
            if payload.get("type") == "access" and isinstance(sub, str) and sub:
                return f"user:{sub}"
            logger.debug("Rate-limit key: falling back to IP (not an access token)")
        # Any decode failure falls back to IP-based limiting below.
        except Exception as exc:  # noqa: BLE001 - fall back to IP limiting
            logger.debug(
                "Rate-limit key: falling back to IP (token decode failed: %s)", exc
            )
    return client_ip_bucket(request)


limiter = Limiter(key_func=_get_user_id_or_ip)
