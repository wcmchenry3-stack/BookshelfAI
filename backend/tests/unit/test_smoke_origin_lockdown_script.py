"""The origin-lockdown smoke script fails on 403 and passes on 200 (#721)."""

import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "smoke_origin_lockdown.sh"


def _serve(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("status,expected_rc", [(200, 0), (403, 1), (500, 1)])
def test_smoke_script_exit_code(status, expected_rc):
    server = _serve(status)
    try:
        result = subprocess.run(
            [str(SCRIPT), f"http://127.0.0.1:{server.server_port}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        server.shutdown()
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert str(status) in result.stdout
