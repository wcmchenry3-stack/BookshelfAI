from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator


class GoogleAuthRequest(BaseModel):
    """Body for POST /auth/google."""

    id_token: str


class TokenResponse(BaseModel):
    """Returned after successful auth."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    """Body for POST /auth/refresh."""

    refresh_token: str


class LogoutRequest(BaseModel):
    """Body for POST /auth/logout."""

    refresh_token: str


class UserRead(BaseModel):
    """Returned by GET /auth/me."""

    id: uuid.UUID
    email: str
    display_name: str | None = None
    avatar_url: str | None = None
    enhanced_scan_credits: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("enhanced_scan_credits", mode="before")
    @classmethod
    def _none_credits_to_zero(cls, v: int | None) -> int:
        # Unflushed User instances haven't had the column default applied yet.
        return v or 0
