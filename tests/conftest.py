"""Fixtures: a fake website, fake DNS and a fake LLM, so tests never touch the network."""

from __future__ import annotations

import copy
from dataclasses import replace

import httpx
import pytest

from app.config import Settings
from app.utils import validators

SITE = "https://brightpath.example.com"

HOME_HTML = """<!doctype html>
<html lang="en"><head>
<title>Web Development Company for Small Businesses | Brightpath Digital</title>
<meta name="description" content="Brightpath Digital builds fast websites and online stores for small businesses in Pune.">
<meta property="og:site_name" content="Brightpath Digital">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"ProfessionalService","name":"Brightpath Digital",
 "telephone":"+91 20 5555 0100","address":{"@type":"PostalAddress","addressLocality":"Pune","addressCountry":"IN"}}
</script>
<script>var tracking = "should never reach the LLM";</script>
<style>.hero{color:red}</style>
</head><body>
<header><nav><a href="/">Home</a><a href="/services">Services</a><a href="/about-us">About us</a>
<a href="/blog/post.pdf">Brochure</a></nav></header>
<div class="cookie-banner">We use cookies. Accept all cookies?</div>
<main>
<h1>Websites that grow small businesses</h1>
<p>We design and develop custom websites, e-commerce stores and landing pages for small businesses and startups.</p>
<h2>Our services</h2>
<ul><li>Custom website development</li><li>E-commerce store setup with Shopify and WooCommerce</li>
<li>Search engine optimization audits</li><li>Website maintenance plans</li></ul>
<p>We design and develop custom websites, e-commerce stores and landing pages for small businesses and startups.</p>
<h2>Why Brightpath</h2><p>Affordable fixed-price packages and launch in 3 weeks.</p>
<a href="mailto:hello@brightpath.example.com">Email us</a>
<a href="https://www.linkedin.com/company/brightpath-digital">LinkedIn</a>
<a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
</main>
<footer>Brightpath Digital, Baner Road, Pune, India</footer>
</body></html>"""

SERVICES_HTML = """<html><head><title>Services | Brightpath Digital</title></head><body><main>
<h1>Services</h1><p>Custom website development using modern frameworks.</p>
<p>Online store development for retail brands.</p></main></body></html>"""

ABOUT_HTML = """<html><head><title>About | Brightpath Digital</title></head><body><main>
<h1>About Brightpath</h1><p>Founded by two developers to help local businesses get online.</p></main></body></html>"""


def site_handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "brightpath.example.com":
        pages = {"/": HOME_HTML, "/services": SERVICES_HTML, "/about-us": ABOUT_HTML}
        if path in pages:
            return httpx.Response(200, html=pages[path])
        if path == "/old":
            return httpx.Response(301, headers={"Location": "/"})
        if path == "/to-internal":
            return httpx.Response(302, headers={"Location": "http://internal.example.com/admin"})
        if path == "/forbidden":
            return httpx.Response(403, text="Forbidden")
        if path == "/file.pdf":
            return httpx.Response(200, content=b"%PDF", headers={"Content-Type": "application/pdf"})
        if path == "/empty":
            return httpx.Response(200, html="<html><body><div id='root'></div><script>app()</script></body></html>")
        return httpx.Response(404, text="not found")
    return httpx.Response(599, text=f"unexpected {request.url}")


FAKE_DNS = {
    "brightpath.example.com": ["93.184.215.14"],
    "internal.example.com": ["10.0.0.5"],
    "rebind.example.com": ["93.184.215.14", "127.0.0.1"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    async def resolve(host: str, port: int) -> list[str]:
        if host in FAKE_DNS:
            return FAKE_DNS[host]
        raise validators.DNSResolutionError(f"Domain '{host}' could not be resolved.")

    monkeypatch.setattr(validators, "resolve_host", resolve)


def make_settings(**overrides) -> Settings:
    base = Settings(gemini_api_key="test-gemini-key", max_subpages=2, cache_ttl_seconds=3600)
    return replace(base, **overrides)


def valid_llm_output() -> dict:
    competitor = {
        "website": None,
        "source": "ai_suggestion",
        "source_url": None,
        "main_service": "Web development",
        "target_market": "SMBs",
        "key_strength": "Established brand",
        "strengths": ["Large portfolio"],
        "weaknesses_or_gaps": ["Higher prices"],
    }
    query = {"intent": "Service Search", "business_relevance": "Core service", "target_audience": "Small business owners"}
    return copy.deepcopy(
        {
            "brand": {"name": "Brightpath Digital", "category": "Web Development", "summary": "Builds websites in Pune."},
            "competitor_analysis": {
                "brand_overview": "Pune-based web agency for small businesses.",
                "main_category": "Web Development",
                "products_and_services": ["Custom websites", "E-commerce stores"],
                "brand_profile": {
                    "main_service": "Web development",
                    "target_market": "Small businesses",
                    "key_strength": "Affordable fixed prices",
                    "opportunity": "Publish case studies",
                },
                "competitors": [
                    {**competitor, "name": "Alpha Web Studio"},
                    {
                        **competitor,
                        "name": "Beta Digital",
                        "source": "web_search",
                        "source_url": "https://made-up.example/x",
                    },
                    {**competitor, "name": "Gamma Sites"},
                    {**competitor, "name": "Brightpath Digital"},  # the brand itself must be removed
                ],
                "competitive_advantages": ["Fast launch"],
                "competitor_gaps": ["Few fixed-price offers"],
                "opportunities": ["Local SEO"],
            },
            "target_search_queries": [
                {**query, "query": "web development company pune", "platforms": ["Google"]},
                {**query, "query": "website for my shop", "platforms": ["Facebook", "Facebook"]},
                {**query, "query": "website design ideas", "platforms": ["Instagram"]},
                {**query, "query": "web agency for startups", "platforms": ["LinkedIn"]},
                {**query, "query": "affordable website developer", "platforms": ["X", "Google"]},
            ],
            "seo_improvements": {
                "primary_keyword": "web development company",
                "secondary_keywords": ["website design pune"],
                "long_tail_keywords": ["affordable web development company for small business"],
                "business_keywords": ["e-commerce website"],
                "recommended_title": "Web Development Company in Pune for SMBs | Brightpath",
                "title_structure": "Primary Keyword – Location | Brand",
                "meta_description": "Brightpath Digital is a web development company in Pune building fast, affordable "
                "websites and online stores for small businesses. Get a quote.",
                "meta_description_keywords": ["web development company"],
                "supporting_content": [
                    {"type": "faq", "title": "Website cost FAQ", "purpose": "Answer pricing", "target_keyword": "cost"}
                ],
            },
            "top_services": [
                {
                    "service_name": "Custom website development",
                    "description": "Bespoke sites",
                    "target_industry": "SMB",
                    "target_customer": "Owners",
                    "main_benefit": "Online presence",
                    "source": "website_content",
                    "evidence": "Custom website development",
                },
                {
                    "service_name": "E-commerce stores",
                    "description": "Shops",
                    "target_industry": "Retail",
                    "target_customer": "Shops",
                    "main_benefit": "Sales",
                    "source": "website_content",
                    "evidence": "invented quote that is not on the page",
                },
                {
                    "service_name": "Mobile app development",
                    "description": "Apps",
                    "target_industry": "SMB",
                    "target_customer": "Owners",
                    "main_benefit": "Reach",
                    "source": "website_content",
                    "evidence": "We build iOS apps",
                },
            ],
            "target_audience": {
                "primary_audience": ["Small business owners"],
                "secondary_audience": ["Startups"],
                "industries": ["Retail"],
                "business_types": ["Local shops"],
                "customer_profiles": ["Owner without a website"],
                "geographic_focus": "Pune, India",
                "customer_needs": ["Affordable website"],
                "customer_challenges": ["No technical team"],
                "customer_interests": ["Online sales"],
                "recommended_segments": [
                    {
                        "segment": "Local retailers",
                        "business_type": "Retail",
                        "requirement": "E-commerce",
                        "recommended_channels": ["Facebook", "Instagram"],
                    }
                ],
            },
        }
    )


@pytest.fixture
def site_client():
    return httpx.AsyncClient(transport=httpx.MockTransport(site_handler))
