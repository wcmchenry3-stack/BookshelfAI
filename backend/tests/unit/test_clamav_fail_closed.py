"""ClamAV scan must fail closed: only an explicit OK result passes (#622)."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.file_security import scan_for_malware


def _scan(result: Any) -> None:
    cd = MagicMock()
    cd.instream.return_value = result
    clamd = MagicMock(ClamdNetworkSocket=MagicMock(return_value=cd))
    with (
        patch("app.core.file_security.settings") as mock_settings,
        patch.dict("sys.modules", {"clamd": clamd}),
    ):
        mock_settings.clamav_enabled = True
        mock_settings.clamav_host = "localhost"
        mock_settings.clamav_port = 3310
        scan_for_malware(b"payload")


def test_ok_passes() -> None:
    _scan({"stream": ("OK", None)})


def test_found_returns_422() -> None:
    with pytest.raises(HTTPException) as exc:
        _scan({"stream": ("FOUND", "Eicar-Test-Signature")})
    assert exc.value.status_code == 422


def test_error_returns_503() -> None:
    with pytest.raises(HTTPException) as exc:
        _scan({"stream": ("ERROR", "size limit exceeded")})
    assert exc.value.status_code == 503


@pytest.mark.parametrize(
    "result",
    [
        {"stream": ("WEIRD", "x")},
        {},
        {"stream": None},
        {"stream": "OK"},
        {"stream": ("OK",)},
        {"stream": ("OK", None, "extra")},
        None,
    ],
    ids=["unexpected_status", "missing", "none", "str", "short", "long", "result_none"],
)
def test_non_ok_or_malformed_returns_503(result: Any) -> None:
    with pytest.raises(HTTPException) as exc:
        _scan(result)
    assert exc.value.status_code == 503
    assert exc.value.detail == "Malware scanning unavailable"
