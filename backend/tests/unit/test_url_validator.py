"""SSRF guard for user-supplied URLs (app/core/url_validator.py).

DNS is stubbed so the tests are deterministic and offline."""

import socket
from unittest.mock import patch

import pytest

from app.core.url_validator import _is_private_ip, validate_safe_url


def _resolves_to(*ips):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",
        "0.0.0.0",
        "255.255.255.255",
        "::1",
        "fc00::1",
        "fe80::1",
        "::ffff:10.0.0.1",
        "::",
    ],
)
def test_blocked_addresses(ip):
    assert _is_private_ip(ip) is True


@pytest.mark.parametrize(
    "ip", ["8.8.8.8", "93.184.216.34", "172.32.0.1", "2606:4700::1"]
)
def test_public_addresses_are_allowed(ip):
    assert _is_private_ip(ip) is False


def test_unparseable_address_is_treated_as_unsafe():
    assert _is_private_ip("not-an-ip") is True


def test_none_passes_through():
    assert validate_safe_url(None) is None


@pytest.mark.parametrize(
    "url", ["http://example.com/a.jpg", "ftp://example.com/a", "file:///etc/passwd"]
)
def test_non_https_schemes_rejected(url):
    with pytest.raises(ValueError, match="https"):
        validate_safe_url(url)


def test_missing_hostname_rejected():
    with pytest.raises(ValueError, match="hostname"):
        validate_safe_url("https:///path")


def test_dns_failure_rejects_rather_than_skips():
    with patch(
        "socket.getaddrinfo", side_effect=socket.gaierror("nxdomain")
    ), pytest.raises(ValueError, match="Could not resolve hostname: nope.test"):
        validate_safe_url("https://nope.test/cover.jpg")


def test_public_host_returns_url_unchanged():
    url = "https://covers.example.com/a.jpg?x=1"
    with patch("socket.getaddrinfo", return_value=_resolves_to("93.184.216.34")):
        assert validate_safe_url(url) == url


def test_host_resolving_to_private_ip_rejected():
    with patch(
        "socket.getaddrinfo", return_value=_resolves_to("10.0.0.5")
    ), pytest.raises(ValueError, match="private or reserved"):
        validate_safe_url("https://internal.example.com/x")


def test_any_private_answer_among_many_rejects():
    # DNS-rebinding style: one public and one private record.
    answers = _resolves_to("93.184.216.34", "169.254.169.254")
    with patch("socket.getaddrinfo", return_value=answers), pytest.raises(
        ValueError, match="private or reserved"
    ):
        validate_safe_url("https://mixed.example.com/x")


def test_ip_literal_in_url_is_checked():
    with patch(
        "socket.getaddrinfo", return_value=_resolves_to("127.0.0.1")
    ), pytest.raises(ValueError):
        validate_safe_url("https://127.0.0.1/x")
