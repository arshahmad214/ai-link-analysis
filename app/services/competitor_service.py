"""Optional web-search grounding for competitor research (Brave Search API).

Without BRAVE_SEARCH_API_KEY the LLM suggests competitors from its own
knowledge and every competitor is labelled `ai_suggestion`.
With the key, real search results are given to the LLM, and a competitor is
labelled `web_search` only when its `source_url` really is one of those
results (checked in analysis_service, not trusted from the model).

Docs: https://api-dashboard.search.brave.com/app/documentation/web-search/get-started
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"


@dataclass
class SearchResult:
    title: str
    url: str
    description: str


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


async def search_competitors(
    client: httpx.AsyncClient, settings: Settings, brand: str, site_url: str, category_hint: str = ""
) -> list[SearchResult]:
    if not settings.brave_search_api_key or not brand:
        return []
    queries = [f"{brand} competitors", f"{brand} alternatives"]
    if category_hint:
        queries.append(f"best {category_hint[:60]} companies")

    async def run(query: str) -> list[SearchResult]:
        try:
            response = await client.get(
                BRAVE_URL,
                params={"q": query, "count": 8},
                headers={"Accept": "application/json", "X-Subscription-Token": settings.brave_search_api_key},
                timeout=10,
            )
            if not response.is_success:
                logger.info("Brave search '%s' failed: HTTP %s", query, response.status_code)
                return []
            items = (response.json().get("web") or {}).get("results") or []
            return [SearchResult(i.get("title", ""), i.get("url", ""), i.get("description", "")) for i in items]
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("Brave search '%s' failed: %s", query, type(exc).__name__)
            return []

    own_host = _host(site_url)
    seen, results = set(), []
    for batch in await asyncio.gather(*(run(q) for q in queries)):
        for item in batch:
            if not item.url or _host(item.url) == own_host or item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
    return results[:15]


def format_results(results: list[SearchResult]) -> str:
    return "\n".join(f"- {r.title} | {r.url} | {r.description[:200]}" for r in results)
