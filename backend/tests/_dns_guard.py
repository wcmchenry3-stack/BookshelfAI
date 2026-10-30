"""No real DNS in the unit and security suites (#778).

A lookup of any non-local host fails the test instead of hitting the network
(which only fails when the machine is offline). Tests that need a lookup patch
``socket.getaddrinfo`` themselves; that patch replaces this guard.
"""

import ipaddress
import socket

import pytest

_LOCAL = {"localhost", "127.0.0.1", "::1", "testserver"}
_real_getaddrinfo = socket.getaddrinfo


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True  # an IP literal needs no DNS


def _guarded_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, bytes):
        host = host.decode()
    if host is None or host in _LOCAL or _is_ip(host):
        return _real_getaddrinfo(host, *args, **kwargs)
    raise AssertionError(f"test did a real DNS lookup of {host!r}; patch it")


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
