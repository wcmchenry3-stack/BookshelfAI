"""Bodies of ``POST /account/export`` (#694)."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# The app's languages; must match app.services.export_email.SUPPORTED_LOCALES
# (tests/unit/test_export_email.py checks).
ExportLocale = Literal[
    "ar", "de", "en", "es", "fr-CA", "he", "hi", "ja", "ko", "nl", "pt", "ru", "zh"
]


class AccountExportRequest(BaseModel):
    """Optional body: the language of the email (the app's current one)."""

    model_config = ConfigDict(extra="forbid")

    locale: ExportLocale = "en"


class AccountExportQueued(BaseModel):
    """202: the export is queued; the link arrives by email."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    created_at: datetime
