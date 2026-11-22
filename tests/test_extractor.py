import asyncio

import pytest

from app.services.website_extractor import WebsiteFetchError, extract_website, parse_html, pick_subpages
from app.utils.validators import BlockedURLError

from .conftest import HOME_HTML, SITE, make_settings


def test_parse_extracts_metadata_and_clean_text():
    page = parse_html(HOME_HTML.encode(), SITE + "/", SITE + "/", 12_000)
    assert page.title.startswith("Web Development Company")
    assert "Pune" in page.meta_description
    assert page.og["site_name"] == "Brightpath Digital"
    assert page.lang == "en"
    assert page.structured_data[0]["name"] == "Brightpath Digital"
    assert page.structured_data[0]["address"] == "Pune, IN"
    assert page.h1 == ["Websites that grow small businesses"]
    assert "Services" in page.nav_items
    assert page.emails == ["hello@brightpath.example.com"]
    assert page.social_links == {"linkedin": "https://www.linkedin.com/company/brightpath-digital"}  # share link ignored
    assert "Pune" in page.footer
    # noise removed
    assert "tracking" not in page.text and ".hero" not in page.text and "cookies" not in page.text
    # duplicate paragraph appears once
    assert page.text.count("We design and develop custom websites") == 1


def test_text_is_truncated_to_budget():
    page = parse_html(HOME_HTML.encode(), SITE, SITE, 80)
    assert len(page.text) <= 80 and page.truncated


def test_subpage_selection_prefers_services_then_about_and_skips_files():
    page = parse_html(HOME_HTML.encode(), SITE + "/", SITE + "/", 12_000)
    assert pick_subpages(page, 2) == [SITE + "/services", SITE + "/about-us"]
    assert pick_subpages(page, 0) == []


def test_extract_website_follows_redirect_and_loads_subpages(site_client):
    content = asyncio.run(extract_website(site_client, SITE + "/old", make_settings()))
    assert content.main.final_url == SITE + "/"
    assert content.pages_analyzed == [SITE + "/", SITE + "/services", SITE + "/about-us"]
    assert content.brand_guess == "Brightpath Digital"
    prompt = content.to_prompt()
    assert "MAIN PAGE TEXT" in prompt and "SUBPAGE https://brightpath.example.com/services" in prompt


def test_redirect_to_internal_address_is_blocked(site_client):
    with pytest.raises(BlockedURLError):
        asyncio.run(extract_website(site_client, SITE + "/to-internal", make_settings()))


@pytest.mark.parametrize(
    "path, code, status",
    [
        ("/forbidden", "website_access_denied", 502),
        ("/missing", "website_not_found", 422),
        ("/file.pdf", "unsupported_content", 422),
        ("/empty", "no_readable_content", 422),
    ],
)
def test_fetch_errors_are_mapped(site_client, path, code, status):
    with pytest.raises(WebsiteFetchError) as exc_info:
        asyncio.run(extract_website(site_client, SITE + path, make_settings(max_subpages=0)))
    assert exc_info.value.code == code and exc_info.value.status_code == status


def test_html_size_limit(site_client):
    content = asyncio.run(extract_website(site_client, SITE + "/", make_settings(max_html_bytes=1500, max_subpages=0)))
    assert any("only the first part" in w for w in content.warnings)
