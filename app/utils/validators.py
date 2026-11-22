"""URL validation and SSRF protection.

Every URL the service fetches (the submitted one, every redirect hop and every
sub-page) goes through `ensure_public_url`, which:

* allows only http/https, rejects credentials in the URL and non-standard ports;
* rejects obviously internal host names (localhost, *.local, *.internal, ...);
* resolves the host name and rejects it if ANY resolved address is not a
  globally routable unicast address (private, loopback, link-local such as the
  169.254.169.254 cloud metadata endpoint, CGNAT, multicast, reserved, ...).
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata", "metadata.google.internal"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")
HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$", re.IGNORECASE)


class InvalidURLError(ValueError):
    """The URL is malformed or not allowed (HTTP 422)."""


class BlockedURLError(ValueError):
    """The URL points to a private/internal destination (HTTP 400)."""


class DNSResolutionError(ValueError):
    """The host name does not resolve (HTTP 422)."""


def normalize_url(raw: str) -> str:
    """Trim, add https:// when the scheme is missing, lowercase scheme/host, drop the fragment."""
    url = (raw or "").strip()
    if not url:
        raise InvalidURLError("URL must not be empty.")
    if len(url) > 2048:
        raise InvalidURLError("URL is longer than 2048 characters.")
    if any(ch.isspace() for ch in url):
        raise InvalidURLError("URL must not contain spaces.")
    if "://" not in url:
        url = "https://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise InvalidURLError(f"Only http and https URLs are supported (got '{parts.scheme}').")
    if parts.username or parts.password:
        raise InvalidURLError("URLs with embedded credentials are not allowed.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise InvalidURLError("URL has no host name.")
    try:
        port = parts.port
    except ValueError:
        raise InvalidURLError("URL has an invalid port.") from None
    try:
        host_ascii = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise InvalidURLError(f"Host name '{host}' is not valid.") from None
    netloc = f"[{host_ascii}]" if ":" in host_ascii else host_ascii
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise BlockedURLError("URL resolves to a private, loopback or otherwise internal address, which is not allowed.")


def check_url_syntax(url: str, allowed_ports: tuple[int, ...] = (80, 443)) -> str:
    """Static checks that do not need DNS. Returns the host name."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in allowed_ports:
        raise BlockedURLError(f"Port {port} is not allowed (allowed: {', '.join(map(str, allowed_ports))}).")
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        _check_ip(literal_ip)
        return host
    if host in BLOCKED_HOSTNAMES or host.endswith(BLOCKED_SUFFIXES):
        raise BlockedURLError(f"Host '{host}' is an internal name, which is not allowed.")
    labels = host.split(".")
    if len(labels) < 2 or not all(HOST_LABEL.match(label) for label in labels) or labels[-1].isdigit():
        raise InvalidURLError(f"Host '{host}' is not a valid public domain name.")
    return host


async def resolve_host(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise DNSResolutionError(f"Domain '{host}' could not be resolved.") from None
    return sorted({info[4][0] for info in infos})


async def ensure_public_url(url: str, allowed_ports: tuple[int, ...] = (80, 443)) -> str:
    """Raise unless the URL is syntactically valid AND every resolved IP is public."""
    host = check_url_syntax(url, allowed_ports)
    try:
        ipaddress.ip_address(host)
        return url  # literal public IP, already checked
    except ValueError:
        pass
    parts = urlsplit(url)
    addresses = await resolve_host(host, parts.port or (443 if parts.scheme == "https" else 80))
    if not addresses:
        raise DNSResolutionError(f"Domain '{host}' could not be resolved.")
    for address in addresses:
        _check_ip(ipaddress.ip_address(address.split("%", 1)[0]))
    return url
