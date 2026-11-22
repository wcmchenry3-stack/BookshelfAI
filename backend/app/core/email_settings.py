"""Settings for transactional email (Resend). Mixed into ``Settings`` so
``app/core/config.py`` stays under the file-length cap."""

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings

# A single bare address (no display name, list or whitespace/CRLF), since it
# ends up in an email header.
_EMAIL_ADDRESS_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)+"
)


class EmailSettings(BaseSettings):
    # Reply-To on transactional email sent through Resend (#678). Resend only
    # sends; replies land in the inbox that Cloudflare Email Routing forwards
    # this address to. Empty = send without a Reply-To header.
    email_reply_to: str = "support@buffingchi.com"

    @field_validator("email_reply_to")
    @classmethod
    def valid_email_reply_to(cls, v: str) -> str:
        v = v.strip()
        if v and not _EMAIL_ADDRESS_PATTERN.fullmatch(v):
            raise ValueError("EMAIL_REPLY_TO must be a single plain email address")
        return v
