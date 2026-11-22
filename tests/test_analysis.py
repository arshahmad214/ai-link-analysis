"""Whole pipeline with a fake LLM: post-validation, caching, repair and fallback."""

import asyncio

import pytest

from app.services import analysis_service
from app.services.llm_service import LLMError, LLMResult
from app.utils.cache import TTLCache

from .conftest import SITE, make_settings, valid_llm_output


class FakeProvider:
    def __init__(self, outputs, name="gemini", model="gemini-3.1-flash-lite"):
        self.outputs, self.name, self.model, self.calls = list(outputs), name, model, []

    async def generate(self, system, user, schema):
        self.calls.append(user)
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, self.name, self.model, 6000, 2500, 1200)


@pytest.fixture
def fake_llm(monkeypatch):
    providers = {}

    def install(**by_name):
        providers.update(by_name)
        monkeypatch.setattr(analysis_service, "build_provider", lambda s, c, name, model="": providers[name])
        return providers

    return install


def analyze(site_client, settings=None, cache=None, url=SITE, refresh=False):
    service = analysis_service.AnalysisService(settings or make_settings(), site_client, cache or TTLCache(3600, 10))
    return asyncio.run(service.analyze(url, refresh=refresh))


def test_full_analysis_and_post_validation(site_client, fake_llm):
    fake_llm(gemini=FakeProvider([valid_llm_output()]))
    result = analyze(site_client)

    assert result.success and result.final_url == SITE + "/"
    assert result.brand.name == "Brightpath Digital"
    ca = result.analysis.competitor_analysis
    names = [c.name for c in ca.competitors]
    assert names == ["Alpha Web Studio", "Beta Digital", "Gamma Sites"]  # brand itself removed
    # claimed web_search without any search results -> relabelled
    assert all(c.source == "ai_suggestion" and c.source_url is None for c in ca.competitors)
    assert "AI-generated suggestions" in ca.disclaimer
    assert [r.attribute for r in ca.comparison_table][:2] == ["Brand Name", "Main Service"]
    assert ca.comparison_table[4].competitors["Alpha Web Studio"] == "Higher prices"

    queries = result.analysis.target_search_queries
    assert len(queries) == 5 and queries[1].platforms == ["Facebook"]  # duplicate removed
    assert queries[4].platforms == ["Google", "X"]

    services = {s.service_name: s for s in result.analysis.top_services}
    assert services["Custom website development"].source == "website_content"
    assert services["Custom website development"].evidence == "Custom website development"
    # quote is invented, but the service name itself appears on the page -> kept, quote dropped
    assert services["E-commerce stores"].source == "website_content"
    assert services["E-commerce stores"].evidence is None
    assert services["Mobile app development"].source == "inferred"

    seo = result.analysis.seo_improvements
    assert seo.current_title.startswith("Web Development Company")
    assert seo.current_title_length == len(seo.current_title)
    assert seo.recommended_title_length == len(seo.recommended_title)

    meta = result.meta
    assert meta.input_tokens == 6000 and meta.output_tokens == 2500
    assert meta.estimated_cost_usd == pytest.approx(0.00525)
    assert meta.competitor_research == "llm_only" and meta.cached is False
    assert len(meta.pages_analyzed) == 3
    assert any("labelled inferred" in w for w in meta.warnings)


def test_prompt_contains_site_content_not_scripts(site_client, fake_llm):
    provider = FakeProvider([valid_llm_output()])
    fake_llm(gemini=provider)
    analyze(site_client)
    prompt = provider.calls[0]
    assert "Websites that grow small businesses" in prompt
    assert "should never reach the LLM" not in prompt
    assert 'source="ai_suggestion"' in prompt  # no search results available


def test_cache_avoids_second_llm_call(site_client, fake_llm):
    provider = FakeProvider([valid_llm_output(), valid_llm_output()])
    fake_llm(gemini=provider)
    cache = TTLCache(3600, 10)
    first = analyze(site_client, cache=cache)
    second = analyze(site_client, cache=cache, url="brightpath.example.com")
    assert len(provider.calls) == 1
    assert first.meta.cached is False and second.meta.cached is True
    analyze(site_client, cache=cache, refresh=True)
    assert len(provider.calls) == 2


def test_invalid_output_is_repaired_once(site_client, fake_llm):
    broken = valid_llm_output()
    del broken["seo_improvements"]
    provider = FakeProvider([broken, valid_llm_output()])
    fake_llm(gemini=provider)
    result = analyze(site_client)
    assert len(provider.calls) == 2 and "did not match the required schema" in provider.calls[1]
    assert result.meta.input_tokens == 12000  # both attempts are counted


def test_fallback_provider_is_used(site_client, fake_llm):
    fake_llm(
        gemini=FakeProvider([LLMError("quota", "llm_rate_limited", 503)]),
        openai=FakeProvider([valid_llm_output()], name="openai", model="gpt-6-luna"),
    )
    result = analyze(site_client, settings=make_settings(llm_fallback_provider="openai"))
    assert result.meta.fallback_used and result.meta.provider == "openai"
    assert any("fallback used" in w for w in result.meta.warnings)


def test_error_without_fallback_propagates(site_client, fake_llm):
    fake_llm(gemini=FakeProvider([LLMError("down", "llm_error", 502)]))
    with pytest.raises(LLMError):
        analyze(site_client)


def test_web_search_backed_competitors_are_kept(site_client, fake_llm, monkeypatch):
    from app.services.competitor_service import SearchResult

    async def fake_search(client, settings, brand, site_url, category_hint=""):
        return [SearchResult("Beta Digital – web agency", "https://made-up.example/x", "Agency in Pune")]

    monkeypatch.setattr(analysis_service, "search_competitors", fake_search)
    provider = FakeProvider([valid_llm_output()])
    fake_llm(gemini=provider)
    result = analyze(site_client)
    beta = next(c for c in result.analysis.competitor_analysis.competitors if c.name == "Beta Digital")
    assert beta.source == "web_search" and beta.source_url == "https://made-up.example/x"
    assert result.meta.competitor_research == "web_search"
    assert "<web_search_results>" in provider.calls[0]
