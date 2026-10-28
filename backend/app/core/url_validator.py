"""SSRF-safe URL validation for user-supplied URL fields.

Validates that a URL:
  1. Uses the https:// scheme only
  2. Resolves to a public IP address (not private/loopback/link-local/reserved)

Raises ValueError on any violation so Pydantic wraps it as a 422 response.
"""

import ipaddress
import socket
from urllib.parse import urlparse

# Private and reserved IP networks per RFC 1918, RFC 4291, RFC 3927, etc.
_BLOCKED_NETWORKS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    # IPv4
    ipaddress.IPv4Network("127.0.0.0/8"),  # loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC 1918 private
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC 1918 private
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC 1918 private
    ipaddress.IPv4Network("169.254.0.0/16"),  # link-local / cloud metadata
    ipaddress.IPv4Network("0.0.0.0/8"),  # "this" network
    ipaddress.IPv4Network("100.64.0.0/10"),  # shared address space (RFC 6598)
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv4Network("198.18.0.0/15"),  # benchmarking (RFC 2544)
    ipaddress.IPv4Network("198.51.100.0/24"),  # TEST-NET-2 (RFC 5737)
    ipaddress.IPv4Network("203.0.113.0/24"),  # TEST-NET-3 (RFC 5737)
    ipaddress.IPv4Network("240.0.0.0/4"),  # reserved (RFC 1112)
    ipaddress.IPv4Network("255.255.255.255/32"),  # broadcast
    # IPv6
    ipaddress.IPv6Network("::1/128"),  # loopback
    ipaddress.IPv6Network("fc00::/7"),  # unique local (RFC 4193)
    ipaddress.IPv6Network("fe80::/10"),  # link-local
    ipaddress.IPv6Network("::ffff:0:0/96"),  # IPv4-mapped
    ipaddress.IPv6Network("::/128"),  # unspecified
]


def _is_private_ip(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        return True  # unparseable — treat as unsafe


def validate_safe_url(url: str | None, *, resolve: bool = True) -> str | None:
    """Validate a user-supplied URL is https and resolves to a public IP.

    Returns the URL unchanged if valid; raises ValueError if not.
    Intended for use as a Pydantic field_validator.

    ``resolve=False`` skips the DNS lookup (a blocking call on the event
    loop) for URLs the server never fetches (#682): only an IP-literal host
    in a blocked range, or ``localhost``, is refused.
    """
    if url is None:
        return url

    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise ValueError("URL must use the https scheme")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must include a valid hostname")

    if not resolve:
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if hostname == "localhost" or hostname.endswith(".localhost"):
                raise ValueError("URL must not point at localhost")
            return url
        if _is_private_ip(hostname):
            raise ValueError(
                "URL points at a private or reserved IP address and is not allowed"
            )
        return url

    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        # DNS resolution failed — reject rather than skip validation
        raise ValueError(f"Could not resolve hostname: {hostname}")

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip = sockaddr[0]
        if _is_private_ip(ip):
            raise ValueError(
                "URL resolves to a private or reserved IP address and is not allowed"
            )

    return url
