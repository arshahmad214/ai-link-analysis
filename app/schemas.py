"""API request/response models and the structured-output schema the LLM must follow."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

Platform = Literal["Google", "Facebook", "Instagram", "LinkedIn", "X"]
SUPPORTED_PLATFORMS: tuple[str, ...] = ("Google", "Facebook", "Instagram", "LinkedIn", "X")


# =============================================================================
#  Request
# =============================================================================
class LinkAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"url": "https://example.com"}]})

    url: str = Field(..., description="Public website URL (http/https). 'example.com' is treated as https://example.com.")
    refresh: bool = Field(False, description="Skip the cache and analyze the website again.")


# =============================================================================
#  LLM output (this schema is sent to the model as a JSON Schema)
#  Keep descriptions short: they are input tokens on every request.
# =============================================================================
class _Strict(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Brand(_Strict):
    name: str = Field(description="Brand/company name as shown on the website")
    category: str = Field(description="Main business category")
    summary: str = Field(description="1-2 sentence business overview")


class BrandProfile(_Strict):
    main_service: str
    target_market: str
    key_strength: str
    opportunity: str = Field(description="Main opportunity to improve market positioning")


class Competitor(_Strict):
    name: str
    website: Optional[str] = Field(description="Homepage URL if known, else null")
    source: Literal["web_search", "ai_suggestion"] = Field(
        description="web_search only if it appears in the provided search results, otherwise ai_suggestion"
    )
    source_url: Optional[str] = Field(description="URL of the search result that supports it, else null")
    main_service: str
    target_market: str
    key_strength: str
    strengths: list[str]
    weaknesses_or_gaps: list[str]


class CompetitorAnalysis(_Strict):
    brand_overview: str
    main_category: str
    products_and_services: list[str] = Field(description="Main products/services found on the website")
    brand_profile: BrandProfile
    competitors: list[Competitor] = Field(description="At least 3 relevant competitors when reliable")
    competitive_advantages: list[str] = Field(description="The submitted brand's advantages vs competitors")
    competitor_gaps: list[str] = Field(description="Gaps in competitors' offering the brand can exploit")
    opportunities: list[str] = Field(description="Concrete actions to improve market positioning")


class SearchQuery(_Strict):
    query: str
    platforms: list[Platform]
    intent: str = Field(description="e.g. Service Search, Business Discovery, Inspiration, B2B Search, Comparison")
    business_relevance: str
    target_audience: str


class SupportingContent(_Strict):
    type: Literal[
        "service_landing_page", "industry_page", "faq", "blog_topic", "location_page", "business_article"
    ]  # fmt: skip
    title: str
    purpose: str
    target_keyword: str


class SEOImprovements(_Strict):
    primary_keyword: str
    secondary_keywords: list[str]
    long_tail_keywords: list[str]
    business_keywords: list[str]
    recommended_title: str = Field(description="50-60 characters, primary keyword near the start, brand at the end")
    title_structure: str = Field(description="Pattern used, e.g. 'Primary Keyword – Benefit | Brand'")
    meta_description: str = Field(description="140-160 characters, human-friendly, includes the primary keyword")
    meta_description_keywords: list[str]
    supporting_content: list[SupportingContent]


class Service(_Strict):
    service_name: str
    description: str
    target_industry: str
    target_customer: str
    main_benefit: str
    source: Literal["website_content", "inferred"] = Field(
        description="website_content if stated on the site, inferred if it is your recommendation"
    )
    evidence: Optional[str] = Field(description="Short exact quote from the website text that mentions it, else null")


class AudienceSegment(_Strict):
    segment: str
    business_type: str
    requirement: str
    recommended_channels: list[Platform]


class TargetAudience(_Strict):
    primary_audience: list[str]
    secondary_audience: list[str]
    industries: list[str]
    business_types: list[str]
    customer_profiles: list[str]
    geographic_focus: Optional[str] = Field(description="Only if stated or clearly implied by the website, else null")
    customer_needs: list[str]
    customer_challenges: list[str]
    customer_interests: list[str]
    recommended_segments: list[AudienceSegment]


class LLMAnalysis(_Strict):
    """The full object the model must return."""

    brand: Brand
    competitor_analysis: CompetitorAnalysis
    target_search_queries: list[SearchQuery] = Field(description="Exactly 5 queries")
    seo_improvements: SEOImprovements
    top_services: list[Service] = Field(description="Up to 5 most important services")
    target_audience: TargetAudience


# =============================================================================
#  API response (LLM output + deterministic additions)
# =============================================================================
class ComparisonRow(BaseModel):
    attribute: str
    your_brand: str
    competitors: dict[str, str]


class CompetitorSection(CompetitorAnalysis):
    comparison_table: list[ComparisonRow] = []
    disclaimer: str = ""


class SEOSection(SEOImprovements):
    current_title: Optional[str] = None
    current_title_length: int = 0
    current_meta_description: Optional[str] = None
    current_meta_description_length: int = 0
    recommended_title_length: int = 0
    meta_description_length: int = 0
    notes: list[str] = []


class AnalysisSection(BaseModel):
    competitor_analysis: CompetitorSection
    target_search_queries: list[SearchQuery]
    search_volume_note: str = (
        "Search volumes are not included: no keyword-volume data source is connected, and volumes are never estimated."
    )
    seo_improvements: SEOSection
    top_services: list[Service]
    target_audience: TargetAudience


class Meta(BaseModel):
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Optional[float] = Field(description="tokens x list price of the model; null if price unknown")
    llm_latency_ms: int
    total_latency_ms: int
    cached: bool = False
    fallback_used: bool = False
    pages_analyzed: list[str]
    content_chars_sent: int
    competitor_research: Literal["web_search", "llm_only"]
    warnings: list[str] = []


class LinkAnalysisResponse(BaseModel):
    success: Literal[True] = True
    website: str
    final_url: str
    brand: Brand
    analysis: AnalysisSection
    meta: Meta


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    website: Optional[str] = None
    error: ErrorBody
