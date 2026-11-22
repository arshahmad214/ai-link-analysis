"""Settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()

# USD per 1M tokens (standard tier, text, short prompts) taken from the official
# pricing pages on 2026-11-22. Used only to *estimate* the cost of each request
# from the token usage the provider reports. Override with LLM_PRICE_INPUT_PER_M /
# LLM_PRICE_OUTPUT_PER_M if prices change or you use another model.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    # Google Gemini – https://ai.google.dev/gemini-api/docs/pricing
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    # OpenAI – https://developers.openai.com/api/docs/models/gpt-6-luna
    "gpt-6-luna": (0.10, 0.50),
    # Anthropic – https://platform.claude.com/docs/en/about-claude/pricing
    "claude-haiku-5-5": (0.10, 0.50),  # prompts <= 100k tokens
    "claude-sonnet-5-5": (2.00, 10.00),
}

DEFAULT_MODELS = {
    "gemini": "gemini-3.1-flash-lite",
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-5-5",
}


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _optional_float(name: str) -> float | None:
    raw = _get(name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Settings:
    # --- LLM ---------------------------------------------------------------
    llm_provider: str = "gemini"  # gemini | openai | anthropic
    llm_model: str = ""  # empty = provider default
    llm_fallback_provider: str = ""  # optional second provider
    llm_fallback_model: str = ""
    llm_max_output_tokens: int = 8000
    llm_timeout: float = 90.0
    openai_reasoning_effort: str = "low"  # none|low|medium|high (reasoning tokens are billed as output)
    gemini_thinking_level: str = ""  # e.g. minimal|low; empty = model default
    price_input_per_m: float | None = None
    price_output_per_m: float | None = None

    gemini_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # --- Competitor research (optional web search) ---------------------------
    brave_search_api_key: str = ""

    # --- Website fetching ----------------------------------------------------
    fetch_timeout: float = 15.0
    max_html_bytes: int = 2_000_000
    max_redirects: int = 5
    max_content_chars: int = 12_000  # main page text sent to the LLM
    max_subpages: int = 2  # about/services pages fetched in addition
    subpage_content_chars: int = 3_000
    allowed_ports: tuple[int, ...] = (80, 443)
    user_agent: str = "Mozilla/5.0 (compatible; AILinkAnalysisBot/1.0; +https://github.com)"

    # --- Misc ----------------------------------------------------------------
    cache_ttl_seconds: int = 86_400
    cache_max_entries: int = 256
    api_key: str = ""  # optional protection for POST /link-analysis
    output_language: str = "English"
    cors_origins: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> Settings:
        ports = tuple(int(p) for p in _get("ALLOWED_PORTS", "80,443").split(",") if p.strip().isdigit())
        origins = tuple(o.strip() for o in _get("CORS_ORIGINS").split(",") if o.strip())
        return cls(
            llm_provider=_get("LLM_PROVIDER", "gemini").lower() or "gemini",
            llm_model=_get("LLM_MODEL"),
            llm_fallback_provider=_get("LLM_FALLBACK_PROVIDER").lower(),
            llm_fallback_model=_get("LLM_FALLBACK_MODEL"),
            llm_max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", 8000),
            llm_timeout=_float("LLM_TIMEOUT_SECONDS", 90.0),
            openai_reasoning_effort=_get("OPENAI_REASONING_EFFORT", "low"),
            gemini_thinking_level=_get("GEMINI_THINKING_LEVEL"),
            price_input_per_m=_optional_float("LLM_PRICE_INPUT_PER_M"),
            price_output_per_m=_optional_float("LLM_PRICE_OUTPUT_PER_M"),
            gemini_api_key=_get("GEMINI_API_KEY"),
            openai_api_key=_get("OPENAI_API_KEY"),
            anthropic_api_key=_get("ANTHROPIC_API_KEY"),
            brave_search_api_key=_get("BRAVE_SEARCH_API_KEY"),
            fetch_timeout=_float("FETCH_TIMEOUT_SECONDS", 15.0),
            max_html_bytes=_int("MAX_HTML_BYTES", 2_000_000),
            max_redirects=_int("MAX_REDIRECTS", 5),
            max_content_chars=_int("MAX_CONTENT_CHARS", 12_000),
            max_subpages=_int("MAX_SUBPAGES", 2),
            subpage_content_chars=_int("SUBPAGE_CONTENT_CHARS", 3_000),
            allowed_ports=ports or (80, 443),
            cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 86_400),
            cache_max_entries=_int("CACHE_MAX_ENTRIES", 256),
            api_key=_get("ANALYSIS_API_KEY"),
            output_language=_get("OUTPUT_LANGUAGE", "English") or "English",
            cors_origins=origins,
        )

    def model_for(self, provider: str, explicit: str = "") -> str:
        return explicit or DEFAULT_MODELS.get(provider, "")

    def price_for(self, model: str) -> tuple[float, float] | None:
        if self.price_input_per_m is not None and self.price_output_per_m is not None:
            return self.price_input_per_m, self.price_output_per_m
        return MODEL_PRICING.get(model)

    def secret_values(self) -> list[str]:
        values = [self.gemini_api_key, self.openai_api_key, self.anthropic_api_key, self.brave_search_api_key, self.api_key]
        return [v for v in values if v and len(v) >= 6]


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
