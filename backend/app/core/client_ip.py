"""The one place the API decides which IP address a request came from.

Every per-IP rate-limit key comes from :func:`client_ip_bucket`, which is built
on :func:`resolve_client_ip`. No other app code reads ``CF-Connecting-IP``,
``X-Forwarded-For`` or ``request.client`` to identify a caller.

Trust model (``settings.trusted_proxy_mode``, env ``TRUSTED_PROXY_MODE``;
when unset it defaults to ``cloudflare`` in hardened environments and ``none``
otherwise):

``none``
    The socket peer. Every forwarding header is ignored. Use this when uvicorn
    is reached directly, with no trusted proxy in front.
``cloudflare``
    ``CF-Connecting-IP`` when the request carries exactly one header with one
    valid IP address; otherwise the socket peer. ``X-Forwarded-For`` is never
    read. The header is only trustworthy on requests that actually came
    through the Cloudflare edge, so this mode belongs with the origin lockdown
    (``ORIGIN_AUTH_SECRET``, see ``app.core.origin_auth``) — see
    ``docs/security.md`` and ``docs/RENDER.md``.

A missing, malformed or ambiguous header never falls back to a more
client-controlled source: it falls through to the socket peer.

The socket peer itself must not be rewritten from forwarding headers by the
server, so the API is started with ``uvicorn --no-proxy-headers``
(``render.yaml``, ``backend/Dockerfile``).

Rate-limit buckets are one per IPv4 address and one per IPv6 /64 (the usual
allocation for a single subscriber), so one client cannot spread its requests
across the addresses of its own /64.
"""

from __future__ import annotations

import ipaddress

from fastapi import Request

from app.core.config import settings

CF_CONNECTING_IP = "cf-connecting-ip"

# Longest textual IPv6 form ("ffff:...:255.255.255.255") is 45 characters.
_MAX_IP_TEXT_LENGTH = 45
IPV6_BUCKET_PREFIX = 64

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def parse_ip(value: str | None) -> IPAddress | None:
    """One IP address in canonical form, or ``None`` if ``value`` isn't exactly one.

    Rejects lists, ports, brackets, zone IDs and surrounding junk. An
    IPv4-mapped IPv6 address is returned as its IPv4 address so both spellings
    of the same client share one bucket.
    """
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > _MAX_IP_TEXT_LENGTH or "%" in text:
        return None
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _peer(request: Request) -> str:
    client = request.client
    if client is None or not client.host:
        return "unknown"
    parsed = parse_ip(client.host)
    return str(parsed) if parsed is not None else client.host


def _cloudflare_ip(request: Request) -> IPAddress | None:
    values = request.headers.getlist(CF_CONNECTING_IP)
    if len(values) != 1:
        return None
    return parse_ip(values[0])


def resolve_client_ip(request: Request) -> str:
    """The client IP for this request, per ``settings.trusted_proxy_mode``."""
    if settings.trusted_proxy_mode == "cloudflare":
        cf_ip = _cloudflare_ip(request)
        if cf_ip is not None:
            return str(cf_ip)
    return _peer(request)


def client_ip_bucket(request: Request) -> str:
    """Rate-limit key for the resolved client IP (IPv6 grouped by /64)."""
    ip = resolve_client_ip(request)
    addr = parse_ip(ip)
    if isinstance(addr, ipaddress.IPv6Address):
        network = ipaddress.IPv6Network((addr, IPV6_BUCKET_PREFIX), strict=False)
        return str(network)
    return ip
