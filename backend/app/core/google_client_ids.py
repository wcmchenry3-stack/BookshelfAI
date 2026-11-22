"""Warn-only boot check for blank Google client IDs (#988).

Kept out of ``config.py`` (file-length cap). Imports nothing from the app, so
``config_check`` can call it on a freshly built ``Settings``.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.config import Settings


_GOOGLE_CLIENT_ID_SLOTS = (
    ("GOOGLE_CLIENT_ID", "google_client_id"),
    ("GOOGLE_IOS_CLIENT_ID", "google_ios_client_id"),
    ("GOOGLE_ANDROID_CLIENT_ID", "google_android_client_id"),
)


def google_client_id_warnings(cfg: "Settings") -> list[str]:
    """Warn-only boot check (#988): a blank Google client-ID slot in a hardened
    environment means sign-ins from that platform fail with an ``aud`` mismatch.

    Pure and never raises. Names only the env var, never a value. Returns []
    in development and test, where the IDs are often intentionally unset.
    """
    if not cfg.is_hardened:
        return []
    return [
        f"{name} is unset: Google sign-in from that platform will be rejected "
        "(aud mismatch)"
        for name, attr in _GOOGLE_CLIENT_ID_SLOTS
        if not getattr(cfg, attr)
    ]
