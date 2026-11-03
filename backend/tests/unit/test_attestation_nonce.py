"""Unit tests for the HMAC nonce used in device attestation (#753)."""

import time
import uuid

import pytest

from app.core.attestation import _NONCE_TTL_SECONDS, issue_nonce, verify_nonce

_SECRET = "test-secret-at-least-32-bytes-long-here"


def _user() -> uuid.UUID:
    return uuid.uuid4()


class TestIssueNonce:
    def test_returns_two_part_token(self):
        nonce = issue_nonce(_user(), _SECRET)
        assert nonce.count(".") == 1

    def test_is_ascii(self):
        nonce = issue_nonce(_user(), _SECRET)
        nonce.encode("ascii")  # no UnicodeEncodeError


class TestVerifyNonce:
    def test_valid_nonce_accepted(self):
        uid = _user()
        nonce = issue_nonce(uid, _SECRET)
        assert verify_nonce(nonce, uid, _SECRET)

    def test_wrong_user_rejected(self):
        nonce = issue_nonce(_user(), _SECRET)
        assert not verify_nonce(nonce, _user(), _SECRET)

    def test_wrong_secret_rejected(self):
        uid = _user()
        nonce = issue_nonce(uid, _SECRET)
        assert not verify_nonce(nonce, uid, "wrong-secret")

    def test_empty_secret_rejected(self):
        uid = _user()
        nonce = issue_nonce(uid, _SECRET)
        assert not verify_nonce(nonce, uid, "")

    def test_tampered_payload_rejected(self):
        uid = _user()
        nonce = issue_nonce(uid, _SECRET)
        payload_b64, sig_b64 = nonce.split(".", 1)
        # Flip last char of payload
        tampered = payload_b64[:-1] + ("A" if payload_b64[-1] != "A" else "B")
        assert not verify_nonce(f"{tampered}.{sig_b64}", uid, _SECRET)

    def test_tampered_signature_rejected(self):
        uid = _user()
        nonce = issue_nonce(uid, _SECRET)
        payload_b64, sig_b64 = nonce.split(".", 1)
        tampered_sig = sig_b64[:-1] + ("A" if sig_b64[-1] != "A" else "B")
        assert not verify_nonce(f"{payload_b64}.{tampered_sig}", uid, _SECRET)

    def test_malformed_nonce_rejected(self):
        uid = _user()
        assert not verify_nonce("notanonce", uid, _SECRET)
        assert not verify_nonce("", uid, _SECRET)
        assert not verify_nonce("a.b.c", uid, _SECRET)

    def test_expired_nonce_rejected(self, monkeypatch):
        uid = _user()
        past = int(time.time()) - _NONCE_TTL_SECONDS - 1
        monkeypatch.setattr("app.core.attestation.time.time", lambda: float(past))
        nonce = issue_nonce(uid, _SECRET)
        # Verify at current time — should be expired.
        monkeypatch.undo()
        assert not verify_nonce(nonce, uid, _SECRET)

    def test_just_within_ttl_accepted(self, monkeypatch):
        uid = _user()
        now = int(time.time())
        monkeypatch.setattr(
            "app.core.attestation.time.time",
            lambda: float(now - _NONCE_TTL_SECONDS + 1),
        )
        nonce = issue_nonce(uid, _SECRET)
        monkeypatch.undo()
        assert verify_nonce(nonce, uid, _SECRET)

    def test_different_nonces_each_call(self):
        uid = _user()
        assert issue_nonce(uid, _SECRET) != issue_nonce(uid, _SECRET)
