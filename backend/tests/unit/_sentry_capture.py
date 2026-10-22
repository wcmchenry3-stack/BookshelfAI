"""A real Sentry SDK client whose events land in a list (no network)."""

import contextlib

import sentry_sdk
from sentry_sdk.transport import Transport

from app.core.sentry_context import sentry_privacy_options


@contextlib.contextmanager
def captured_events():
    """Yield a list that collects every event payload the SDK would send.

    Runs in forked scopes and with the app's privacy options, like
    tests/unit/test_sentry_scrubbing.py's ``captured`` fixture.
    """
    payloads: list[dict] = []

    class _Capture(Transport):
        def capture_envelope(self, envelope):
            for item in envelope.items:
                if item.headers.get("type") == "event":
                    payloads.append(item.payload.json)

    with sentry_sdk.isolation_scope(), sentry_sdk.new_scope():
        client = sentry_sdk.Client(
            dsn="https://abc@o1.ingest.us.sentry.io/1",
            transport=_Capture,
            send_default_pii=False,
            default_integrations=False,
            **sentry_privacy_options(),
        )
        sentry_sdk.get_global_scope().set_client(client)
        try:
            yield payloads
        finally:
            client.close()
            sentry_sdk.get_global_scope().set_client(None)
