"""Settings for private object storage (Supabase Storage, #694). Mixed into
``Settings`` so ``app/core/config.py`` stays under the file-length cap. Not
meant to be instantiated on its own: ``Settings`` supplies the env-file,
blank-value and error-redaction config.

Both ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` unset (the default)
is the account export's kill switch: ``POST /account/export`` answers 503
``export_not_configured`` and the export worker does nothing
(``app/services/account_export_jobs.py``). The service-role key is a secret:
read from the environment only, never logged, sent only to ``SUPABASE_URL``
(``app/services/object_storage.py``).
"""

import re
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Supabase bucket ids: lower-case letters, digits, dots, hyphens, underscores.
_BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9._\-]{1,62}")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class StorageSettings(BaseSettings):
    # The project URL, e.g. https://<ref>.supabase.co. Empty = storage off.
    supabase_url: str = ""
    # The project's service-role key (bypasses storage policies; server only).
    supabase_service_role_key: str = ""
    # A PRIVATE bucket for account exports (#694); objects are served only
    # through signed URLs and deleted after 7 days.
    export_bucket: str = "account-exports"

    @field_validator("supabase_url")
    @classmethod
    def valid_supabase_url(cls, v: str) -> str:
        """Empty, or ``https://host[:port]`` with no credentials, path, query
        or fragment (``http`` only for a local host). Messages describe the
        rule only; the value is never echoed."""
        v = v.strip().rstrip("/")
        if not v:
            return v
        try:
            parts = urlsplit(v)
            host = parts.hostname
            parts.port  # noqa: B018 - raises ValueError on a bad port
        except ValueError:
            raise ValueError("SUPABASE_URL must be an https URL") from None
        if (
            not host
            or parts.username is not None
            or parts.password is not None
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError("SUPABASE_URL must be https://<host> with nothing else")
        if parts.scheme != "https" and not (
            parts.scheme == "http" and host in _LOCAL_HOSTS
        ):
            raise ValueError("SUPABASE_URL must be an https URL")
        return v

    @field_validator("supabase_service_role_key")
    @classmethod
    def strip_service_role_key(cls, v: str) -> str:
        return v.strip()

    @field_validator("export_bucket")
    @classmethod
    def valid_export_bucket(cls, v: str) -> str:
        v = v.strip()
        if not _BUCKET_PATTERN.fullmatch(v):
            raise ValueError("EXPORT_BUCKET must be a Supabase bucket id")
        return v
