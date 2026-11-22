"""Fetches a website safely and extracts clean, de-duplicated business content.

The output is deliberately compact: it is what we send to the LLM, so every
character removed here (scripts, menus repeated on every page, cookie banners,
duplicate lines) is a token we do not pay for.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.utils.validators import ensure_public_url, normalize_url

logger = logging.getLogger(__name__)

SOCIAL_HOSTS = {
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "linkedin.com": "linkedin",
    "twitter.com": "x",
    "x.com": "x",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "pinterest.com": "pinterest",
}
ORG_TYPES = {
    "organization", "corporation", "localbusiness", "professionalservice", "store",
    "onlinestore", "onlinebusiness", "restaurant", "medicalorganization", "educationalorganization",
    "ngo", "website", "brand",
}  # fmt: skip
SUBPAGE_KEYWORDS = {
    "services": ("service", "solution", "product", "what-we-do", "offering", "feature", "pricing", "plans"),
    "about": ("about", "company", "who-we-are", "our-story", "team"),
}
NOISE_PATTERN = re.compile(r"cookie|consent|gdpr|newsletter-popup|modal", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".mp3", ".doc", ".docx")


class WebsiteFetchError(Exception):
    def __init__(self, message: str, code: str, status_code: int = 502):
        super().__init__(message)
        self.message, self.code, self.status_code = message, code, status_code


@dataclass
class ExtractedPage:
    url: str
    final_url: str
    title: str = ""
    meta_description: str = ""
    meta_keywords: str = ""
    og: dict[str, str] = field(default_factory=dict)
    lang: str = ""
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    h3: list[str] = field(default_factory=list)
    nav_items: list[str] = field(default_factory=list)
    text: str = ""
    footer: str = ""
    structured_data: list[dict] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    social_links: dict[str, str] = field(default_factory=dict)
    internal_links: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False


@dataclass
class WebsiteContent:
    main: ExtractedPage
    subpages: list[ExtractedPage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def pages_analyzed(self) -> list[str]:
        return [self.main.final_url] + [p.final_url for p in self.subpages]

    @property
    def brand_guess(self) -> str:
        for item in self.main.structured_data:
            if item.get("name"):
                return str(item["name"])
        if self.main.og.get("site_name"):
            return self.main.og["site_name"]
        host = (urlsplit(self.main.final_url).hostname or "").removeprefix("www.")
        domain_label = host.split(".")[0]
        segments = [s.strip() for s in re.split(r"\s+[|\-–—:•·]\s+", self.main.title or "") if s.strip()]
        for segment in segments:  # "Web Design Agency | Acme" -> the segment that matches the domain
            compact = re.sub(r"[^a-z0-9]", "", segment.lower())
            if compact and (compact in domain_label or domain_label in compact):
                return segment
        if segments:
            return segments[0]
        return host

    def all_text(self) -> str:
        """Everything the LLM sees, used to check that 'evidence' quotes are real."""
        parts = [self.main.title, self.main.meta_description, *self.main.h1, *self.main.h2, *self.main.h3, self.main.text]
        for page in self.subpages:
            parts += [page.title, *page.h1, *page.h2, page.text]
        return "\n".join(p for p in parts if p)

    def to_prompt(self) -> str:
        m = self.main
        lines = [f"URL: {m.final_url}"]
        if m.lang:
            lines.append(f"Language: {m.lang}")
        if m.title:
            lines.append(f"Title tag: {m.title}")
        if m.meta_description:
            lines.append(f"Meta description: {m.meta_description}")
        if m.meta_keywords:
            lines.append(f"Meta keywords: {m.meta_keywords}")
        for key in ("site_name", "title", "description"):
            if m.og.get(key) and m.og[key] not in (m.title, m.meta_description):
                lines.append(f"og:{key}: {m.og[key]}")
        if m.structured_data:
            lines.append("Structured data (JSON-LD): " + json.dumps(m.structured_data, ensure_ascii=False)[:1500])
        if m.h1:
            lines.append("H1: " + " | ".join(m.h1))
        if m.h2:
            lines.append("H2: " + " | ".join(m.h2))
        if m.h3:
            lines.append("H3: " + " | ".join(m.h3))
        if m.nav_items:
            lines.append("Navigation menu: " + " | ".join(m.nav_items))
        if m.emails or m.phones:
            lines.append("Contact: " + ", ".join(m.emails + m.phones))
        if m.social_links:
            lines.append("Social profiles: " + ", ".join(f"{k}: {v}" for k, v in m.social_links.items()))
        if m.footer:
            lines.append(f"Footer: {m.footer}")
        lines += ["", "MAIN PAGE TEXT:", m.text or "(no readable text found)"]
        for page in self.subpages:
            lines += ["", f"SUBPAGE {page.final_url}" + (f" — {page.title}" if page.title else "") + ":"]
            if page.h1 or page.h2:
                lines.append("Headings: " + " | ".join(page.h1 + page.h2))
            lines.append(page.text)
        return "\n".join(lines)


# --------------------------------------------------------------------------- fetch
async def fetch_html(client: httpx.AsyncClient, url: str, settings: Settings) -> tuple[str, bytes, str, bool]:
    """GET the URL following redirects manually so that every hop is SSRF-checked.

    Returns (final_url, body_bytes, charset, truncated).
    """
    current = url
    for _ in range(settings.max_redirects + 1):
        await ensure_public_url(current, settings.allowed_ports)
        try:
            async with client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=settings.fetch_timeout,
                headers={
                    "User-Agent": settings.user_agent,
                    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en;q=0.9,*;q=0.5",
                },
            ) as response:
                if response.is_redirect and response.headers.get("location"):
                    current = normalize_url(urljoin(current, response.headers["location"]))
                    continue
                _raise_for_status(response)
                content_type = response.headers.get("content-type", "").lower()
                if content_type and not any(t in content_type for t in ("html", "xml", "text/plain")):
                    raise WebsiteFetchError(
                        f"The URL does not point to a web page (content type '{content_type.split(';')[0]}').",
                        "unsupported_content",
                        422,
                    )
                body, truncated = bytearray(), False
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= settings.max_html_bytes:
                        truncated = True
                        del body[settings.max_html_bytes :]
                        break
                return str(response.url), bytes(body), response.charset_encoding or "", truncated
        except httpx.TimeoutException:
            raise WebsiteFetchError("The website did not respond in time.", "website_timeout", 504) from None
        except httpx.ConnectError:
            raise WebsiteFetchError(
                "Could not connect to the website (connection refused, DNS or TLS error).", "website_unreachable", 502
            ) from None
        except httpx.HTTPError as exc:
            raise WebsiteFetchError(
                f"Error while fetching the website: {type(exc).__name__}.", "website_error", 502
            ) from None
    raise WebsiteFetchError(f"Too many redirects (more than {settings.max_redirects}).", "too_many_redirects", 502)


def _raise_for_status(response: httpx.Response) -> None:
    status = response.status_code
    if status < 400:
        return
    if status in (401, 403):
        raise WebsiteFetchError(
            f"The website refused access (HTTP {status}). It may require login or block automated requests.",
            "website_access_denied",
            502,
        )
    if status in (404, 410):
        raise WebsiteFetchError(f"Page not found (HTTP {status}).", "website_not_found", 422)
    if status == 429:
        raise WebsiteFetchError(
            "The website is rate-limiting requests (HTTP 429). Try again later.", "website_rate_limited", 502
        )
    raise WebsiteFetchError(f"The website returned an error (HTTP {status}).", "website_error", 502)


# --------------------------------------------------------------------------- parse
def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _unique(items, limit: int) -> list[str]:
    seen, out = set(), []
    for item in items:
        item = _clean(item)
        key = item.lower()
        if item and key not in seen and len(item) <= 200:
            seen.add(key)
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _meta(soup: BeautifulSoup, **attrs) -> str:
    tag = soup.find("meta", attrs=attrs)
    return _clean(tag.get("content")) if isinstance(tag, Tag) else ""


def _same_site(host_a: str, host_b: str) -> bool:
    def strip(host: str) -> str:
        return host.lower().removeprefix("www.")

    return strip(host_a) == strip(host_b)


def _walk_json_ld(node, found: list[dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_json_ld(item, found)
        return
    if not isinstance(node, dict):
        return
    if "@graph" in node:
        _walk_json_ld(node["@graph"], found)
    types = node.get("@type", [])
    types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
    if any(str(t).lower() in ORG_TYPES for t in types):
        fact = {"type": types[0] if types else ""}
        for key in ("name", "legalName", "description", "slogan", "telephone", "email", "areaServed", "priceRange"):
            value = node.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                fact[key] = _clean(str(value))[:300]
        address = node.get("address")
        if isinstance(address, dict):
            parts = [address.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "addressCountry")]
            parts = [p if isinstance(p, str) else (p or {}).get("name", "") if isinstance(p, dict) else "" for p in parts]
            if any(parts):
                fact["address"] = ", ".join(p for p in parts if p)
        if len(fact) > 1:
            found.append(fact)


def _is_noise_container(tag: Tag) -> bool:
    """Cookie banners, consent dialogs, pop-ups: matched by class or id."""
    if tag.name in ("html", "body", "main", "article"):
        return False  # e.g. <body class="cookie-consent-open"> must stay
    classes = tag.get("class") or []
    label = " ".join(classes) if isinstance(classes, list) else str(classes)
    return bool(NOISE_PATTERN.search(f"{label} {tag.get('id') or ''}"))


def _extract_text(container: Tag, max_chars: int) -> tuple[str, bool]:
    lines, seen, total = [], set(), 0
    for raw in container.get_text("\n").split("\n"):
        line = _clean(raw)
        if len(line) < 3 or not re.search(r"[A-Za-zÀ-￿]", line):
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        if total + len(line) > max_chars:
            return "\n".join(lines), True
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines), False


def parse_html(html: bytes | str, url: str, final_url: str, max_chars: int, charset: str = "") -> ExtractedPage:
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "lxml", from_encoding=charset or None)
    else:
        soup = BeautifulSoup(html, "lxml")
    page = ExtractedPage(url=url, final_url=final_url)
    page.title = _clean(soup.title.get_text()) if soup.title else ""
    page.meta_description = _meta(soup, name=re.compile(r"^description$", re.I))
    page.meta_keywords = _meta(soup, name=re.compile(r"^keywords$", re.I))[:300]
    for prop in ("site_name", "title", "description", "type"):
        value = _meta(soup, property=f"og:{prop}")
        if value:
            page.og[prop] = value
    html_tag = soup.find("html")
    page.lang = _clean(html_tag.get("lang")) if isinstance(html_tag, Tag) else ""

    # Structured data, before scripts are removed.
    found: list[dict] = []
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        try:
            _walk_json_ld(json.loads(script.string or script.get_text() or "{}"), found)
        except (ValueError, TypeError):
            continue
    page.structured_data = found[:3]

    # Links: internal (for sub-pages), social profiles, contact.
    base_host = urlsplit(final_url).hostname or ""
    internal: list[tuple[str, str]] = []
    emails, phones = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            emails.append(href[7:].split("?")[0])
            continue
        if href.startswith("tel:"):
            phones.append(href[4:])
            continue
        absolute = urldefrag(urljoin(final_url, href))[0]
        parts = urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        host = parts.hostname.lower().removeprefix("www.")
        for social_host, name in SOCIAL_HOSTS.items():
            if (host == social_host or host.endswith("." + social_host)) and name not in page.social_links:
                if parts.path.strip("/") and "share" not in parts.path and "intent" not in parts.path:
                    page.social_links[name] = absolute
        if _same_site(parts.hostname, base_host) and not parts.path.lower().endswith(SKIP_EXTENSIONS):
            internal.append((absolute, _clean(a.get_text())))
    page.internal_links = internal[:300]
    page.phones = _unique(phones, 3)

    page.nav_items = _unique((a.get_text() for a in soup.select("nav a, header a")), 25)
    footer = soup.find("footer")
    if isinstance(footer, Tag):
        page.footer = _clean(footer.get_text(" "))[:400]

    # Remove noise before extracting readable text.
    noise = soup(["script", "style", "noscript", "template", "svg", "iframe", "canvas", "form", "nav", "footer"])
    noise += soup.find_all(attrs={"aria-hidden": "true"})
    noise += soup.find_all(_is_noise_container)
    for tag in noise:
        if not tag.decomposed:  # may already be gone together with its parent
            tag.decompose()

    page.h1 = _unique((h.get_text(" ") for h in soup.find_all("h1")), 5)
    page.h2 = _unique((h.get_text(" ") for h in soup.find_all("h2")), 15)
    page.h3 = _unique((h.get_text(" ") for h in soup.find_all("h3")), 15)

    container = soup.find("main") or soup.find("article") or soup.body or soup
    text, truncated = _extract_text(container, max_chars)
    if len(text) < 300 and soup.body is not None and container is not soup.body:
        text, truncated = _extract_text(soup.body, max_chars)
    page.text, page.truncated = text, truncated
    page.emails = _unique(emails + EMAIL_PATTERN.findall(text), 3)
    return page


def pick_subpages(page: ExtractedPage, limit: int) -> list[str]:
    """Choose the most informative internal pages (services first, then about)."""
    if limit <= 0:
        return []
    main_path = urlsplit(page.final_url).path.rstrip("/")
    chosen: list[str] = []
    for kind in ("services", "about"):
        for url, text in page.internal_links:
            parts = urlsplit(url)
            path = parts.path.rstrip("/")
            haystack = f"{path} {text}".lower()
            if path == main_path or parts.query or path.count("/") > 3 or url in chosen:
                continue
            if any(k in haystack for k in SUBPAGE_KEYWORDS[kind]):
                chosen.append(url)
                break
        if len(chosen) >= limit:
            break
    return chosen[:limit]


async def extract_website(client: httpx.AsyncClient, url: str, settings: Settings) -> WebsiteContent:
    final_url, body, charset, truncated = await fetch_html(client, url, settings)
    main = parse_html(body, url, final_url, settings.max_content_chars, charset)
    content = WebsiteContent(main=main)
    if truncated:
        content.warnings.append(f"HTML larger than {settings.max_html_bytes} bytes; only the first part was analyzed.")

    async def load(sub_url: str) -> ExtractedPage | None:
        try:
            sub_final, sub_body, sub_charset, _ = await fetch_html(client, sub_url, settings)
            return parse_html(sub_body, sub_url, sub_final, settings.subpage_content_chars, sub_charset)
        except Exception as exc:  # sub-pages are best effort
            logger.info("Skipping sub-page %s: %s", sub_url, exc)
            return None

    sub_urls = pick_subpages(main, settings.max_subpages)
    if sub_urls:
        pages = await asyncio.gather(*(load(u) for u in sub_urls))
        seen_text = {main.text}
        for sub in pages:
            if sub and sub.text and sub.text not in seen_text:
                content.subpages.append(sub)
                seen_text.add(sub.text)

    readable = len(main.text) + sum(len(p.text) for p in content.subpages)
    if readable < 200:
        if not (main.title or main.meta_description or main.og):
            raise WebsiteFetchError(
                "The page has no readable content. It may be rendered entirely with JavaScript or be empty.",
                "no_readable_content",
                422,
            )
        content.warnings.append(
            "Very little readable text was found (the site may render content with JavaScript); "
            "the analysis relies mostly on page metadata and may be less precise."
        )
    return content
