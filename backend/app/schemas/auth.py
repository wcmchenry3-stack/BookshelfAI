from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


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
    created_at: datetime

    model_config = {"from_attributes": True}
