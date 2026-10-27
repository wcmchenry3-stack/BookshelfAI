"""The log-hygiene checker (#692) flags what it should and only that."""

import pytest

from tests._log_hygiene import check_texts

EMAIL = "someone@leaktest.example"
ALLOWED = "owner@leaktest.example"
SUB = "112233445566778899001"
# A fake, unsigned JWT-shaped value, joined at runtime so secret scanners
# don't mistake the literal for a real token.
_HEADER, _PAYLOAD, _SIG = "eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiIxMjM0In0", "c2lnbmF0dXJl"
JWT = f"{_HEADER}.{_PAYLOAD}.{_SIG}"


def kinds(text, **kw):
    return {f.kind for f in check_texts([text], source="log", **kw)}


@pytest.mark.parametrize(
    "text, kw, expected",
    [
        (f"user {EMAIL} signed in", {"emails": [EMAIL]}, "email"),
        ("user a.b+c@sub.example.org signed in", {}, "email"),
        (f"allow {ALLOWED}", {"allowlist": [ALLOWED]}, "allowlist"),
        (f"token={JWT}", {}, "jwt"),
        ("access token abc123refresh", {"secrets": ["abc123refresh"]}, "secret"),
        (f"google sub {SUB}", {"secrets": [SUB]}, "secret"),
        ("sent Bearer abcdefgh12345678", {}, "authorization"),
        ('{"authorization": "Basic dXNlcjpwdw=="}', {}, "authorization"),
        ("Authorization: opaque-credential-value", {}, "authorization"),
    ],
)
def test_flags(text, kw, expected):
    assert expected in kinds(text, **kw)


@pytest.mark.parametrize(
    "text",
    [
        "Auth successful (user_id=00000000-0000-0000-0000-000000000001)",
        "Email not in allowlist",
        "Google account email not verified",
        "POST /auth/google - verifying Google ID token",
        "app.api.auth.google_auth took 12ms (version 1.2.3)",
        '{"headers": {"authorization": "[Filtered]"}}',
        "Authorization: [REDACTED]",
        'File "/x/site-packages/starlette/routing.py", line 1, in app',
    ],
)
def test_clean_text_passes(text):
    assert check_texts([text], source="log") == []


def test_matching_is_case_insensitive_for_known_values():
    assert kinds(f"X {EMAIL.upper()}", emails=[EMAIL])


def test_finding_never_prints_the_value():
    finding, *_ = check_texts([f"x {EMAIL}"], source="sentry", emails=[EMAIL])
    assert EMAIL not in str(finding) and EMAIL not in repr(finding)
    assert str(finding) == "email found in sentry"


def test_url_encoded_known_values_are_flagged():
    assert kinds("GET /x?email=someone%40leaktest.example", emails=[EMAIL])
    assert kinds("GET /x?e=a%2Bb%40y.example", allowlist=["a+b@y.example"])
    assert kinds("q=some%20secret", secrets=["some secret"])
