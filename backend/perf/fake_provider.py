"""A fake AI provider for the nightly /scan load run (#539).

A small stdlib HTTP server (no third-party imports) that answers the two
outbound calls ``POST /scan`` makes, so the load run never reaches a real
model or a real catalog:

- ``POST /v1/messages``: the Anthropic Messages API, in the shape the
  Claude adapter (``app/ai/providers/anthropic_vision.py``) parses. A fixed
  ``end_turn`` reply with two books and fixed usage numbers. The request body
  is read and dropped (the image is never stored or logged).
- ``GET /search.json``: Open Library, always an empty result list, so
  enrichment makes no outbound call (``OPEN_LIBRARY_BASE_URL`` points here).
- ``GET /__count``: what the load run asserts on, as JSON:
  ``calls`` (every Messages request), ``errors`` (those answered with the
  injected error), ``in_flight`` and ``max_in_flight`` (concurrent requests),
  and the ``input_tokens`` / ``output_tokens`` every reply reports (the load
  run prices a call from these).
- ``POST /__reset``: zero the counters and restore the start-up latency and
  error rate.
- ``POST /__config``: change ``latency_ms`` and ``error_rate`` while running.

Run: ``python perf/fake_provider.py --port 18080 --latency-ms 200``. The
settings can also come from ``FAKE_PROVIDER_*`` environment variables.

It lives under ``backend/perf/``, not ``backend/app/``: nothing in the app
imports it, and the app reaches it only through the validated
``AI_ANTHROPIC_BASE_URL`` setting.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Two real books with valid ISBNs, so the adapter's parser keeps them all.
BOOKS = [
    {
        "title": "Dune",
        "author": "Frank Herbert",
        "confidence": 0.97,
        "isbn_13": "9780441013593",
        "isbn_10": "0441013597",
    },
    {
        "title": "The Great Gatsby",
        "author": "F. Scott Fitzgerald",
        "confidence": 0.9,
        "isbn_13": "9780743273565",
        "isbn_10": None,
    },
]

DEFAULT_INPUT_TOKENS = 30_000
DEFAULT_OUTPUT_TOKENS = 8_000
# 4xx: the adapter does not retry it (it retries 429/500/503/529), so one
# injected error is exactly one provider request and the user's scan is refunded.
ERROR_STATUS = 400


class State:
    """Counters and settings, shared by the handler threads."""

    def __init__(
        self,
        latency_ms: float = 0.0,
        error_rate: float = 0.0,
        input_tokens: int = DEFAULT_INPUT_TOKENS,
        output_tokens: int = DEFAULT_OUTPUT_TOKENS,
    ) -> None:
        self.lock = threading.Lock()
        self.latency_ms = latency_ms
        self.error_rate = error_rate
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        # What /__reset restores, so a run never inherits a previous
        # scenario's latency or error rate.
        self._initial = (latency_ms, error_rate)
        self.calls = 0
        self.errors = 0
        self.in_flight = 0
        self.max_in_flight = 0

    def reset(self) -> None:
        with self.lock:
            self.calls = self.errors = self.in_flight = self.max_in_flight = 0
            self.latency_ms, self.error_rate = self._initial

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "calls": self.calls,
                "errors": self.errors,
                "in_flight": self.in_flight,
                "max_in_flight": self.max_in_flight,
                "latency_ms": self.latency_ms,
                "error_rate": self.error_rate,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
            }


def messages_reply(state: State) -> dict:
    """The Messages API body for one successful call."""
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps({"books": BOOKS})}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": state.input_tokens,
            "output_tokens": state.output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format, *args):
            pass

        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/__count":
                self._send(200, state.snapshot())
            elif path == "/search.json":
                self._send(200, {"docs": []})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            body = self._read_body()
            if path == "/__reset":
                state.reset()
                self._send(200, state.snapshot())
            elif path == "/__config":
                try:
                    config = json.loads(body or b"{}")
                    with state.lock:
                        if "latency_ms" in config:
                            state.latency_ms = max(0.0, float(config["latency_ms"]))
                        if "error_rate" in config:
                            state.error_rate = min(
                                1.0, max(0.0, float(config["error_rate"]))
                            )
                except (ValueError, TypeError):
                    self._send(400, {"error": "bad config"})
                    return
                self._send(200, state.snapshot())
            elif path == "/v1/messages":
                self._messages()
            else:
                self._send(404, {"error": "not found"})

        def _messages(self) -> None:
            with state.lock:
                state.calls += 1
                state.in_flight += 1
                state.max_in_flight = max(state.max_in_flight, state.in_flight)
                latency = state.latency_ms
                fail = random.random() < state.error_rate
                if fail:
                    state.errors += 1
            try:
                if latency:
                    time.sleep(latency / 1000.0)
                if fail:
                    self._send(
                        ERROR_STATUS,
                        {
                            "type": "error",
                            "error": {"type": "invalid_request_error", "message": "x"},
                        },
                    )
                else:
                    self._send(200, messages_reply(state))
            finally:
                with state.lock:
                    state.in_flight -= 1

    return Handler


def make_server(
    port: int = 0, host: str = "127.0.0.1", state: State | None = None
) -> tuple[ThreadingHTTPServer, State]:
    """A ready (not yet serving) server and its state. ``port=0`` picks a free
    port; read it from ``server.server_address[1]``."""
    state = state or State()
    server = ThreadingHTTPServer((host, port), make_handler(state))
    server.daemon_threads = True
    return server, state


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=_env_float("FAKE_PROVIDER_PORT", 18080)
    )
    parser.add_argument(
        "--latency-ms", type=float, default=_env_float("FAKE_PROVIDER_LATENCY_MS", 0)
    )
    parser.add_argument(
        "--error-rate", type=float, default=_env_float("FAKE_PROVIDER_ERROR_RATE", 0)
    )
    parser.add_argument(
        "--input-tokens",
        type=int,
        default=int(_env_float("FAKE_PROVIDER_INPUT_TOKENS", DEFAULT_INPUT_TOKENS)),
    )
    parser.add_argument(
        "--output-tokens",
        type=int,
        default=int(_env_float("FAKE_PROVIDER_OUTPUT_TOKENS", DEFAULT_OUTPUT_TOKENS)),
    )
    args = parser.parse_args(argv)
    state = State(
        args.latency_ms, args.error_rate, args.input_tokens, args.output_tokens
    )
    server, _ = make_server(args.port, args.host, state)
    print(f"fake provider on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
