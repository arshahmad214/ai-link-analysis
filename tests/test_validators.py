import asyncio

import pytest

from app.utils.validators import (
    BlockedURLError,
    DNSResolutionError,
    InvalidURLError,
    ensure_public_url,
    normalize_url,
)


def check(url: str) -> str:
    return asyncio.run(ensure_public_url(normalize_url(url)))


def test_normalize_adds_scheme_lowercases_and_drops_fragment():
    assert normalize_url("  BrightPath.Example.com/About#team ") == "https://brightpath.example.com/About"
    assert normalize_url("http://brightpath.example.com") == "http://brightpath.example.com/"


@pytest.mark.parametrize(
    "url, error",
    [
        ("", InvalidURLError),
        ("ftp://example.com", InvalidURLError),
        ("javascript:alert(1)", InvalidURLError),
        ("https://user:pass@example.com", InvalidURLError),
        ("https://exa mple.com", InvalidURLError),
        ("https://example.com:99999", InvalidURLError),
    ],
)
def test_invalid_urls(url, error):
    with pytest.raises(error):
        normalize_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/admin",
        "http://127.0.0.1/",
        "http://10.1.2.3/",
        "http://192.168.0.1/",
        "http://172.16.5.4/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://0.0.0.0/",
        "http://printer.local/",
        "http://metadata.google.internal/",
        "https://brightpath.example.com:8080/",  # non-standard port
        "http://internal.example.com/",  # public name resolving to 10.0.0.5
        "http://rebind.example.com/",  # one of the answers is loopback
    ],
)
def test_ssrf_targets_are_blocked(url):
    with pytest.raises(BlockedURLError):
        check(url)


def test_unresolvable_domain():
    with pytest.raises(DNSResolutionError):
        check("https://does-not-exist.example.org")


@pytest.mark.parametrize("url", ["http://2130706433/", "https://intranet/", "https://example.123/"])
def test_non_domain_hosts_are_rejected(url):
    with pytest.raises((InvalidURLError, BlockedURLError)):
        check(url)


def test_public_url_passes():
    assert check("brightpath.example.com") == "https://brightpath.example.com/"
    assert check("http://8.8.8.8/") == "http://8.8.8.8/"
