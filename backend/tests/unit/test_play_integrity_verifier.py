"""Unit tests for LiveAndroidIntegrityVerifier — Standard requestHash (#1050).

The app uses the Play Integrity **Standard** request (``@expo/app-integrity``),
which carries the server nonce in ``tokenPayloadExternal.requestDetails
.requestHash``. Google's ``decodeIntegrityToken`` call is mocked with an
``httpx.MockTransport``; the access-token exchange is stubbed out.
"""

import json
import time
import uuid

import httpx
import pytest

from app.api import _bot_check
from app.core import attestation
from app.core.attestation import (
    _NONCE_TTL_SECONDS,
    LiveAndroidIntegrityVerifier,
    issue_nonce,
)

_SECRET = f"nonce-secret-{uuid.uuid5(uuid.NAMESPACE_URL, 'bookshelf-1050')}"
_PACKAGE = "com.buffingchi.bookshelf"
_USER = uuid.uuid5(uuid.NAMESPACE_URL, "bookshelf-1050-user")
# Play Integrity Standard requests allow a requestHash of at most 500 chars.
_MAX_REQUEST_HASH_LEN = 500


def _standard_verdict(request_hash: str) -> dict:
    """A decoded Standard request verdict, shaped like Google's response."""
    return {
        "tokenPayloadExternal": {
            "requestDetails": {
                "requestPackageName": _PACKAGE,
                "requestHash": request_hash,
                "timestampMillis": str(int(time.time() * 1000)),
            },
            "appIntegrity": {
                "appRecognitionVerdict": "PLAY_RECOGNIZED",
                "packageName": _PACKAGE,
                "certificateSha256Digest": ["ZmFrZS1kaWdlc3Q"],
                "versionCode": "42",
            },
            "deviceIntegrity": {
                "deviceRecognitionVerdict": ["MEETS_DEVICE_INTEGRITY"],
            },
            "accountDetails": {"appLicensingVerdict": "LICENSED"},
        }
    }


def _classic_verdict(nonce: str) -> dict:
    """A decoded Classic request verdict: ``nonce`` instead of ``requestHash``."""
    verdict = _standard_verdict("")
    details = verdict["tokenPayloadExternal"]["requestDetails"]
    del details["requestHash"]
    details["nonce"] = nonce
    return verdict


@pytest.fixture
def google(monkeypatch):
    """Mock the Google decode call; returns a dict to set the verdict and see calls."""
    state: dict = {"verdict": {}, "calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(request)
        return httpx.Response(200, json=state["verdict"])

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    async def fake_access_token(_creds: str) -> str:
        return "stub-access"

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(attestation, "_google_access_token", fake_access_token)
    monkeypatch.setattr(
        "app.core.config.settings.attestation_google_credentials_json", "{}"
    )
    return state


async def _verify(token_nonce: str) -> bool:
    return await LiveAndroidIntegrityVerifier().verify_token(
        token="opaque-integrity-token", nonce=token_nonce, package_name=_PACKAGE
    )


class TestStandardRequestHash:
    async def test_matching_request_hash_passes(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(nonce)
        assert await _verify(nonce) is True
        assert len(google["calls"]) == 1
        call = google["calls"][0]
        assert call.url.path == f"/v1/{_PACKAGE}:decodeIntegrityToken"
        assert json.loads(call.content) == {"integrity_token": "opaque-integrity-token"}

    async def test_mismatching_request_hash_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(issue_nonce(_USER, _SECRET))
        assert await _verify(nonce) is False

    async def test_request_hash_prefix_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(nonce[:-1])
        assert await _verify(nonce) is False

    async def test_missing_request_hash_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        del verdict["tokenPayloadExternal"]["requestDetails"]["requestHash"]
        google["verdict"] = verdict
        assert await _verify(nonce) is False

    @pytest.mark.parametrize("bad", ["", None, 123, ["x"]])
    async def test_non_string_or_empty_request_hash_rejected(self, google, bad):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        verdict["tokenPayloadExternal"]["requestDetails"]["requestHash"] = bad
        google["verdict"] = verdict
        assert await _verify(nonce) is False

    async def test_non_object_request_details_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        verdict["tokenPayloadExternal"]["requestDetails"] = nonce
        google["verdict"] = verdict
        assert await _verify(nonce) is False

    async def test_classic_nonce_only_rejected(self, google):
        """Classic tokens are not accepted: no client sends them (#1050)."""
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _classic_verdict(nonce)
        assert await _verify(nonce) is False

    async def test_classic_nonce_does_not_rescue_bad_request_hash(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict("something-else")
        verdict["tokenPayloadExternal"]["requestDetails"]["nonce"] = nonce
        google["verdict"] = verdict
        assert await _verify(nonce) is False


class TestOtherChecksUnchanged:
    async def test_wrong_package_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        verdict["tokenPayloadExternal"]["requestDetails"][
            "requestPackageName"
        ] = "com.evil.app"
        google["verdict"] = verdict
        assert await _verify(nonce) is False

    async def test_app_not_play_recognized_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        verdict["tokenPayloadExternal"]["appIntegrity"][
            "appRecognitionVerdict"
        ] = "UNRECOGNIZED_VERSION"
        google["verdict"] = verdict
        assert await _verify(nonce) is False

    async def test_device_without_integrity_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        verdict = _standard_verdict(nonce)
        verdict["tokenPayloadExternal"]["deviceIntegrity"][
            "deviceRecognitionVerdict"
        ] = []
        google["verdict"] = verdict
        assert await _verify(nonce) is False


class TestNonceFitsRequestHash:
    def test_issued_nonce_within_standard_request_hash_limit(self):
        # Longest realistic case: current time; the payload length is fixed.
        nonce = issue_nonce(uuid.uuid4(), _SECRET)
        assert len(nonce) <= _MAX_REQUEST_HASH_LEN


class TestVerifyAttestationAndroid:
    """End-to-end through ``_verify_attestation`` (nonce HMAC + expiry first)."""

    @pytest.fixture(autouse=True)
    def _settings(self, monkeypatch):
        monkeypatch.setattr(_bot_check.settings, "attestation_nonce_secret", _SECRET)
        monkeypatch.setattr(
            _bot_check.settings, "attestation_android_package_name", _PACKAGE
        )

    async def _run(self, nonce: str) -> bool:
        return await _bot_check._verify_attestation(
            platform="android",
            key_id=None,
            assertion_b64=None,
            integrity_token="opaque-integrity-token",
            nonce=nonce,
            user_id=_USER,
            db=None,  # type: ignore[arg-type] - unused on the Android path
        )

    async def test_valid_standard_token_passes(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(nonce)
        assert await self._run(nonce) is True

    async def test_expired_nonce_rejected_before_google(self, google, monkeypatch):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(nonce)
        later = time.time() + _NONCE_TTL_SECONDS + 5
        monkeypatch.setattr(attestation.time, "time", lambda: later)
        assert await self._run(nonce) is False
        assert google["calls"] == []

    async def test_forged_nonce_rejected_before_google(self, google):
        nonce = issue_nonce(_USER, "a-different-secret-value")
        google["verdict"] = _standard_verdict(nonce)
        assert await self._run(nonce) is False
        assert google["calls"] == []

    async def test_other_users_nonce_rejected_before_google(self, google):
        nonce = issue_nonce(uuid.uuid4(), _SECRET)
        google["verdict"] = _standard_verdict(nonce)
        assert await self._run(nonce) is False
        assert google["calls"] == []

    async def test_token_for_other_nonce_rejected(self, google):
        nonce = issue_nonce(_USER, _SECRET)
        google["verdict"] = _standard_verdict(issue_nonce(_USER, _SECRET))
        assert await self._run(nonce) is False
