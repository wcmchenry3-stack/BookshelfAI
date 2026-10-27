"""The origin-lockdown smoke script fails on 403 and passes on 200 (#721)."""

import os
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


def _run(*args: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {
        **os.environ,
        "no_proxy": "127.0.0.1,localhost",
        "NO_PROXY": "127.0.0.1,localhost",
        **env,
    }
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env=full_env,
    )


@pytest.mark.parametrize("status,expected_rc", [(200, 0), (403, 1), (500, 1)])
def test_smoke_script_exit_code(status, expected_rc):
    server = _serve(status)
    try:
        result = _run(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == expected_rc, result.stdout + result.stderr
    assert str(status) in result.stdout


def test_smoke_script_connection_refused_fails():
    server = _serve(200)
    port = server.server_port
    server.shutdown()
    server.server_close()
    result = _run(f"http://127.0.0.1:{port}")
    assert result.returncode == 1
    assert "000" in result.stdout


@pytest.mark.parametrize("arg", ["ftp://example.com", "file:///etc/passwd", "-o/tmp/x"])
def test_smoke_script_rejects_bad_base_url(arg):
    result = _run(arg)
    assert result.returncode == 2
    assert "usage" in result.stderr


def test_smoke_script_rejects_bad_path():
    result = _run("http://127.0.0.1:1", SMOKE_PATH="health/db")
    assert result.returncode == 2


def test_smoke_script_never_prints_userinfo():
    server = _serve(403)
    try:
        result = _run(f"http://user:hunter2@127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 1
    assert "hunter2" not in result.stdout + result.stderr
    assert "user:" not in result.stdout + result.stderr
