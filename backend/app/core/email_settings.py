"""Settings for transactional email (Resend). Mixed into ``Settings`` so
``app/core/config.py`` stays under the file-length cap. Not meant to be
instantiated on its own: ``Settings`` supplies the env-file, blank-value and
error-redaction config."""

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
# A single bare address (no display name, list or whitespace/CRLF), since it
# ends up in an email header. Alphabetic TLD, RFC length limits.
_EMAIL_ADDRESS_PATTERN = re.compile(
    rf"[A-Za-z0-9!#$%&'*+/=?^_`{{|}}~\-]+(\.[A-Za-z0-9!#$%&'*+/=?^_`{{|}}~\-]+)*"
    rf"@({_LABEL}\.)+[A-Za-z]{{2,63}}"
)
_MAX_LENGTH = 254
_MAX_LOCAL_LENGTH = 64


class EmailSettings(BaseSettings):
    # Reply-To on transactional email sent through Resend (#678). Resend only
    # sends; replies land in the inbox that Cloudflare Email Routing forwards
    # this address to. A blank env var is treated as unset (env_ignore_empty),
    # so the default applies; there is no way to disable it from the environment.
    email_reply_to: str = "support@buffingchi.com"

    @field_validator("email_reply_to")
    @classmethod
    def valid_email_reply_to(cls, v: str) -> str:
        v = v.strip()
        if (
            len(v) > _MAX_LENGTH
            or len(v.partition("@")[0]) > _MAX_LOCAL_LENGTH
            or not _EMAIL_ADDRESS_PATTERN.fullmatch(v)
        ):
            raise ValueError("EMAIL_REPLY_TO must be a single plain email address")
        return v
