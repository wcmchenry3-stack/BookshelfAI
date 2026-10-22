"""ai_settings (#526) are server-only: no request field, header or query can
set or influence any key, and the module stays out of the provider layer.

Run on its own with:  pytest -m security tests/security/test_ai_settings_authority.py
"""

import ast
import inspect
import io
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app as app_package
from app.ai import settings as ai_settings
from app.ai.providers.base import ProviderResult
from app.ai.registry import FEATURES
from app.main import app
from app.models.user import User

pytestmark = pytest.mark.security

APP_ROOT = Path(app_package.__file__).resolve().parent
USER = User(
    id=uuid.UUID("00000000-0000-0000-0000-00000000a526"),
    email="sec@example.com",
    enhanced_scan_credits=3,
)

# Every key, and the short names a client might guess, as request inputs.
_HOSTILE = {
    **{key: "x" for key in ai_settings.KNOWN_KEYS},
    "model": "gpt-4o",
    "provider": "anthropic",
    "max_image_pixels": "100000000",
    "enabled": "true",
}


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24)).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def client():
    from app.auth.dependencies import get_current_user
    from app.core.database import get_db

    async def _fake_db():
        yield AsyncMock()

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = _fake_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def provider():
    with (
        patch("app.ai.gateway.OpenAIVisionProvider") as provider_cls,
        patch("app.api.scan.EnrichmentService"),
        patch("app.api.scan.DeduplicationService"),
        patch("app.api.scan._reserve_scan_quota", new=AsyncMock()),
        patch("app.api.scan._refund_scan_quota", new=AsyncMock()),
        patch("app.api.scan._release_scan_lease", new=AsyncMock()),
    ):
        provider_cls.return_value.identify = AsyncMock(return_value=ProviderResult([]))
        yield provider_cls


@pytest.mark.parametrize("enhanced", [False, True])
def test_request_fields_headers_and_query_cannot_change_settings(
    client, provider, enhanced
):
    """Form fields, query params and headers named after every key are ignored."""
    ai_settings.reset_cache({"scan.max_image_pixels": 1_000_000})
    before = dict(ai_settings._cache.rows)
    headers = {f"X-{k.replace('.', '-')}": str(v) for k, v in _HOSTILE.items()}
    resp = client.post(
        "/scan",
        params=_HOSTILE,
        headers=headers,
        files={"file": ("s.jpg", io.BytesIO(_jpeg()), "image/jpeg")},
        data={**_HOSTILE, "enhanced": str(enhanced).lower()},
    )
    assert resp.status_code == 200
    feature = "scan.enhanced" if enhanced else "scan.standard"
    provider.assert_called_once_with(FEATURES[feature])
    assert dict(ai_settings._cache.rows) == before


def test_disabled_switch_cannot_be_reenabled_by_the_request(client, provider):
    ai_settings.reset_cache({"scan.standard.enabled": False})
    resp = client.post(
        "/scan",
        params={"scan.standard.enabled": "true"},
        headers={"X-Scan-Standard-Enabled": "true"},
        files={"file": ("s.jpg", io.BytesIO(_jpeg()), "image/jpeg")},
        data={"scan.standard.enabled": "true", "enabled": "true"},
    )
    assert resp.json() == {"detail": "scan_disabled"}
    provider.assert_not_called()


def test_readers_take_only_a_db_session():
    assert list(inspect.signature(ai_settings.get_ai_settings).parameters) == ["db"]


_WRITERS = {"set_setting", "seed_ai_settings", "reset_cache", "AISetting", "insert"}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "app.ai.settings",
            "app.models.ai_settings",
        ):
            names.update(a.name for a in node.names)
        if isinstance(node, ast.Attribute) and node.attr in _WRITERS - {"insert"}:
            names.add(node.attr)
    return names


def test_no_router_can_write_ai_settings():
    """Routers may read the settings, never write them."""
    for path in sorted((APP_ROOT / "api").rglob("*.py")):
        leaked = _imported_names(path) & _WRITERS
        assert not leaked, f"{path.name} reaches ai_settings writers: {leaked}"


def test_settings_module_does_not_import_providers():
    tree = ast.parse((APP_ROOT / "ai" / "settings.py").read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            if node.module == "app.ai":
                modules.update(f"app.ai.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    assert not {m for m in modules if m.startswith("app.ai.providers")}
