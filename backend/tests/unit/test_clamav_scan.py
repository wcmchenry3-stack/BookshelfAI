"""scan_for_malware: the optional ClamAV step fails closed (503) on every
daemon problem and rejects (422) only on a real detection."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core import file_security
from app.core.file_security import scan_for_malware


def _fake_clamd(instream_result=None, instream_exc=None):
    """A stand-in ``clamd`` module whose network socket returns the given reply."""
    daemon = MagicMock()
    if instream_exc is not None:
        daemon.instream.side_effect = instream_exc
    else:
        daemon.instream.return_value = instream_result
    module = types.SimpleNamespace(ClamdNetworkSocket=MagicMock(return_value=daemon))
    return module, daemon


@pytest.fixture
def clamav_on():
    with (
        patch.object(file_security.settings, "clamav_enabled", True),
        patch.object(file_security.settings, "clamav_host", "clam.internal"),
        patch.object(file_security.settings, "clamav_port", 3310),
    ):
        yield


def test_disabled_never_touches_the_daemon():
    module, daemon = _fake_clamd(("FOUND", "Eicar"))
    with (
        patch.object(file_security.settings, "clamav_enabled", False),
        patch.dict(sys.modules, {"clamd": module}),
    ):
        scan_for_malware(b"data")
    module.ClamdNetworkSocket.assert_not_called()
    daemon.instream.assert_not_called()


def test_clean_stream_passes_and_uses_configured_endpoint(clamav_on):
    module, daemon = _fake_clamd({"stream": ("OK", None)})
    with patch.dict(sys.modules, {"clamd": module}):
        scan_for_malware(b"data")
    module.ClamdNetworkSocket.assert_called_once_with(
        host="clam.internal", port=3310, timeout=10
    )
    # The bytes are streamed to the daemon unchanged.
    assert daemon.instream.call_args.args[0].read() == b"data"


def test_missing_clamd_package_fails_closed(clamav_on):
    # A None entry in sys.modules makes ``import clamd`` raise ImportError.
    with patch.dict(sys.modules, {"clamd": None}), pytest.raises(HTTPException) as exc:
        scan_for_malware(b"data")
    assert exc.value.status_code == 503
    assert exc.value.detail == "Malware scanning unavailable"


def test_threat_found_is_rejected_with_422(clamav_on):
    module, _ = _fake_clamd({"stream": ("FOUND", "Eicar-Test-Signature")})
    with patch.dict(sys.modules, {"clamd": module}), pytest.raises(
        HTTPException
    ) as exc:
        scan_for_malware(b"data")
    assert exc.value.status_code == 422
    assert exc.value.detail == "File failed malware scan"


def test_scan_error_status_fails_closed(clamav_on):
    module, _ = _fake_clamd({"stream": ("ERROR", "size limit exceeded")})
    with patch.dict(sys.modules, {"clamd": module}), pytest.raises(
        HTTPException
    ) as exc:
        scan_for_malware(b"data")
    assert exc.value.status_code == 503


def test_reply_without_stream_key_is_treated_as_error(clamav_on):
    module, _ = _fake_clamd({})
    with patch.dict(sys.modules, {"clamd": module}), pytest.raises(
        HTTPException
    ) as exc:
        scan_for_malware(b"data")
    assert exc.value.status_code == 503


@pytest.mark.parametrize("error", [ConnectionRefusedError, TimeoutError, OSError])
def test_daemon_connection_failures_fail_closed(clamav_on, error):
    module, _ = _fake_clamd(instream_exc=error("down"))
    with patch.dict(sys.modules, {"clamd": module}), pytest.raises(
        HTTPException
    ) as exc:
        scan_for_malware(b"data")
    assert exc.value.status_code == 503
    assert exc.value.detail == "Malware scanning unavailable"
