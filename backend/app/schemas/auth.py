from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

# The "Open on" screens (#913); must match app.models.user.DEFAULT_TABS
# (tests/unit/test_user_preferences.py checks). None means Scan.
DefaultTab = Literal["scan", "wishlist", "my-books"]


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
    default_tab: DefaultTab | None = None

    model_config = {"from_attributes": True}


class UserPreferences(BaseModel):
    """Returned by PATCH /users/me/preferences: the user's current settings."""

    default_tab: DefaultTab | None = None

    model_config = {"from_attributes": True}


class UserPreferencesUpdate(BaseModel):
    """Body for PATCH /users/me/preferences (#913).

    Only the fields sent change; ``null`` clears one (``default_tab: null``
    means Scan). Any other field, or a value outside ``DefaultTab``, is 422.
    """

    default_tab: DefaultTab | None = None

    model_config = {"extra": "forbid"}
