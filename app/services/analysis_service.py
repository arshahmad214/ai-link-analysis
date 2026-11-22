"""Orchestrates: validate URL → extract website → (optional) web search → LLM → verify → response."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.schemas import (
    SUPPORTED_PLATFORMS,
    AnalysisSection,
    ComparisonRow,
    CompetitorSection,
    LinkAnalysisResponse,
    LLMAnalysis,
    Meta,
    SEOSection,
)
from app.services import prompts
from app.services.competitor_service import SearchResult, format_results, search_competitors
from app.services.llm_service import LLMError, LLMResult, build_provider, estimate_cost, portable_json_schema
from app.services.website_extractor import WebsiteContent, extract_website
from app.utils.cache import TTLCache
from app.utils.validators import ensure_public_url, normalize_url

logger = logging.getLogger(__name__)
ANALYSIS_SCHEMA = portable_json_schema(LLMAnalysis)


@dataclass
class LLMRun:
    analysis: LLMAnalysis
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost: float | None = 0.0
    fallback_used: bool = False
    warnings: list[str] = field(default_factory=list)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _host(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def _url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    return f"{_host(url)}{parts.path.rstrip('/')}"


class AnalysisService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, cache: TTLCache):
        self.settings, self.client, self.cache = settings, client, cache

    # ------------------------------------------------------------------ public
    async def analyze(self, raw_url: str, refresh: bool = False) -> LinkAnalysisResponse:
        started = time.perf_counter()
        url = normalize_url(raw_url)
        await ensure_public_url(url, self.settings.allowed_ports)

        model = self.settings.model_for(self.settings.llm_provider, self.settings.llm_model)
        key = f"{self.settings.llm_provider}:{model}:{url}"

        if not refresh and (hit := self.cache.get(key)):
            return self._from_cache(hit, started)
        async with self.cache.lock(key):
            if not refresh and (hit := self.cache.get(key)):  # filled while we waited
                return self._from_cache(hit, started)
            response = await self._analyze_uncached(url, started)
            self.cache.set(key, response)
            return response

    # ----------------------------------------------------------------- helpers
    def _from_cache(self, cached: LinkAnalysisResponse, started: float) -> LinkAnalysisResponse:
        response = cached.model_copy(deep=True)
        response.meta.cached = True
        response.meta.total_latency_ms = int((time.perf_counter() - started) * 1000)
        return response

    async def _analyze_uncached(self, url: str, started: float) -> LinkAnalysisResponse:
        content = await extract_website(self.client, url, self.settings)
        search_results = await search_competitors(self.client, self.settings, content.brand_guess, content.main.final_url)

        website_text = content.to_prompt()
        user_prompt = prompts.USER_TEMPLATE.format(
            website=website_text,
            search_block=(
                prompts.SEARCH_BLOCK.format(results=format_results(search_results))
                if search_results
                else prompts.NO_SEARCH_BLOCK
            ),
        )
        system_prompt = prompts.SYSTEM_PROMPT.format(language=self.settings.output_language)
        run = await self._run_llm(system_prompt, user_prompt)

        warnings = [*content.warnings, *run.warnings]
        analysis = run.analysis
        competitor_section = self._competitors(analysis, content, search_results, warnings)
        queries = self._queries(analysis, warnings)
        services = self._services(analysis, content, warnings)
        seo = self._seo(analysis, content)

        return LinkAnalysisResponse(
            website=url,
            final_url=content.main.final_url,
            brand=analysis.brand,
            analysis=AnalysisSection(
                competitor_analysis=competitor_section,
                target_search_queries=queries,
                seo_improvements=seo,
                top_services=services,
                target_audience=analysis.target_audience,
            ),
            meta=Meta(
                provider=run.provider,
                model=run.model,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                estimated_cost_usd=run.cost,
                llm_latency_ms=run.latency_ms,
                total_latency_ms=int((time.perf_counter() - started) * 1000),
                fallback_used=run.fallback_used,
                pages_analyzed=content.pages_analyzed,
                content_chars_sent=len(website_text),
                competitor_research="web_search" if search_results else "llm_only",
                warnings=warnings,
            ),
        )

    async def _run_llm(self, system: str, user: str) -> LLMRun:
        """Primary provider, one schema-repair retry, then the fallback provider if configured."""
        s = self.settings
        chain = [(s.llm_provider, s.llm_model)]
        if s.llm_fallback_provider:
            chain.append((s.llm_fallback_provider, s.llm_fallback_model))

        totals = {"in": 0, "out": 0, "latency": 0, "cost": 0.0}
        cost_known = True
        last_error: LLMError | None = None

        def account(result: LLMResult) -> None:
            nonlocal cost_known
            totals["in"] += result.input_tokens
            totals["out"] += result.output_tokens
            totals["latency"] += result.latency_ms
            cost = estimate_cost(s, result.model, result.input_tokens, result.output_tokens)
            if cost is None:
                cost_known = False
            else:
                totals["cost"] += cost

        for index, (provider_name, model_name) in enumerate(chain):
            try:
                provider = build_provider(s, self.client, provider_name, model_name)
                result = await provider.generate(system, user, ANALYSIS_SCHEMA)
                account(result)
                try:
                    analysis = LLMAnalysis.model_validate(result.data)
                except ValidationError as exc:
                    errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10])
                    logger.warning("%s output failed validation, asking for a repair: %s", provider_name, errors)
                    repair_user = user + "\n\n" + prompts.REPAIR_PROMPT.format(errors=errors)
                    result = await provider.generate(system, repair_user, ANALYSIS_SCHEMA)
                    account(result)
                    try:
                        analysis = LLMAnalysis.model_validate(result.data)
                    except ValidationError:
                        raise LLMError(
                            f"{provider_name} output did not match the schema.", "llm_invalid_output", 502
                        ) from None
                warnings = [f"Primary LLM failed ({last_error.message}); fallback used."] if index and last_error else []
                return LLMRun(
                    analysis=analysis,
                    provider=result.provider,
                    model=result.model,
                    input_tokens=totals["in"],
                    output_tokens=totals["out"],
                    latency_ms=totals["latency"],
                    cost=round(totals["cost"], 6) if cost_known else None,
                    fallback_used=index > 0,
                    warnings=warnings,
                )
            except LLMError as exc:
                logger.warning("LLM %s failed: %s", provider_name, exc.message)
                last_error = exc
        assert last_error is not None
        raise last_error

    # ---------------------------------------------------- deterministic checks
    def _competitors(
        self, analysis: LLMAnalysis, content: WebsiteContent, results: list[SearchResult], warnings: list[str]
    ) -> CompetitorSection:
        ca = analysis.competitor_analysis
        own_host = _host(content.main.final_url)
        brand_key = _normalize_text(analysis.brand.name)
        result_keys = {_url_key(r.url) for r in results}
        result_hosts = {_host(r.url) for r in results}

        kept, seen, downgraded = [], set(), 0
        for comp in ca.competitors:
            name_key = _normalize_text(comp.name)
            if (
                not name_key
                or name_key in seen
                or name_key == brand_key
                or (comp.website and _host(comp.website) == own_host)
            ):
                continue
            seen.add(name_key)
            if comp.source == "web_search":
                supported = comp.source_url and (
                    _url_key(comp.source_url) in result_keys or _host(comp.source_url) in result_hosts
                )
                if not supported:
                    comp.source, comp.source_url = "ai_suggestion", None
                    downgraded += 1
            else:
                comp.source_url = None
            kept.append(comp)
        if downgraded:
            warnings.append(
                f"{downgraded} competitor(s) were not found in the search results and are labelled ai_suggestion."
            )
        if len(kept) < 3:
            warnings.append("Fewer than 3 reliable competitors could be identified.")

        researched = sum(c.source == "web_search" for c in kept)
        if not kept:
            disclaimer = "No competitors could be identified reliably."
        elif researched == len(kept):
            disclaimer = "Competitors were identified from live web search results (see source_url)."
        elif researched:
            disclaimer = (
                "Competitors marked web_search are backed by live search results; those marked ai_suggestion "
                "are AI-generated suggestions and should be verified."
            )
        else:
            disclaimer = (
                "Competitors are AI-generated suggestions based on the website content and the model's general "
                "knowledge (no web search was used). Verify them before making decisions."
            )

        profile = ca.brand_profile
        rows = [
            ComparisonRow(
                attribute="Brand Name", your_brand=analysis.brand.name, competitors={c.name: c.name for c in kept}
            ),
            ComparisonRow(
                attribute="Main Service", your_brand=profile.main_service, competitors={c.name: c.main_service for c in kept}
            ),
            ComparisonRow(
                attribute="Target Market",
                your_brand=profile.target_market,
                competitors={c.name: c.target_market for c in kept},
            ),
            ComparisonRow(
                attribute="Key Strength", your_brand=profile.key_strength, competitors={c.name: c.key_strength for c in kept}
            ),
            ComparisonRow(
                attribute="Opportunity / Gap",
                your_brand=profile.opportunity,
                competitors={c.name: (c.weaknesses_or_gaps[0] if c.weaknesses_or_gaps else "") for c in kept},
            ),
        ]
        data = ca.model_dump()
        data["competitors"] = [c.model_dump() for c in kept]
        return CompetitorSection(**data, comparison_table=rows, disclaimer=disclaimer)

    def _queries(self, analysis: LLMAnalysis, warnings: list[str]):
        queries = []
        for q in analysis.target_search_queries[:5]:
            q.platforms = [p for p in SUPPORTED_PLATFORMS if p in set(q.platforms)]  # dedupe, fixed order
            queries.append(q)
        if len(queries) < 5:
            warnings.append(f"Only {len(queries)} search queries were generated (5 expected).")
        missing = [p for p in SUPPORTED_PLATFORMS if not any(p in q.platforms for q in queries)]
        if missing:
            warnings.append(f"No query targets: {', '.join(missing)}.")
        return queries

    def _services(self, analysis: LLMAnalysis, content: WebsiteContent, warnings: list[str]):
        site_text = _normalize_text(content.all_text())
        services, relabelled = [], 0
        for service in analysis.top_services[:5]:
            if service.source == "website_content":
                evidence = _normalize_text(service.evidence or "")
                if evidence and evidence in site_text:
                    pass  # verified quote
                elif _normalize_text(service.service_name) in site_text:
                    service.evidence = None  # the service is named on the site, quote was not exact
                else:
                    service.source, service.evidence = "inferred", None
                    relabelled += 1
            else:
                service.evidence = None
            services.append(service)
        if relabelled:
            warnings.append(f"{relabelled} service(s) could not be matched to the website text and are labelled inferred.")
        return services

    def _seo(self, analysis: LLMAnalysis, content: WebsiteContent) -> SEOSection:
        seo = analysis.seo_improvements
        current_title = content.main.title or None
        current_meta = content.main.meta_description or None
        notes = []
        if not current_title:
            notes.append("The page has no <title> tag.")
        elif len(current_title) > 60:
            notes.append(f"Current title is {len(current_title)} characters; Google typically truncates around 60.")
        elif len(current_title) < 30:
            notes.append(f"Current title is only {len(current_title)} characters; it can carry more keywords.")
        if not current_meta:
            notes.append("The page has no meta description; search engines will pick text from the page.")
        elif len(current_meta) > 160:
            notes.append(f"Current meta description is {len(current_meta)} characters; aim for 140-160.")
        elif len(current_meta) < 70:
            notes.append(f"Current meta description is only {len(current_meta)} characters; aim for 140-160.")
        if not content.main.h1:
            notes.append("The page has no H1 heading.")
        elif len(content.main.h1) > 1:
            notes.append(f"The page has {len(content.main.h1)} H1 headings; one clear H1 is recommended.")
        if seo.primary_keyword and _normalize_text(seo.primary_keyword) not in _normalize_text(seo.recommended_title):
            notes.append("The recommended title does not contain the exact primary keyword.")
        if not 30 <= len(seo.recommended_title) <= 65:
            notes.append(f"Recommended title is {len(seo.recommended_title)} characters (target 50-60).")
        if not 110 <= len(seo.meta_description) <= 165:
            notes.append(f"Recommended meta description is {len(seo.meta_description)} characters (target 140-160).")
        return SEOSection(
            **seo.model_dump(),
            current_title=current_title,
            current_title_length=len(current_title or ""),
            current_meta_description=current_meta,
            current_meta_description_length=len(current_meta or ""),
            recommended_title_length=len(seo.recommended_title),
            meta_description_length=len(seo.meta_description),
            notes=notes,
        )
