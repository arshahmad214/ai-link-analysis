"""Measure real latency, token usage and cost of each LLM on the same websites.

Every model gets exactly the same extracted content and prompt, so the numbers are comparable.
Only providers whose API key is set in .env are run.

Usage:
    python scripts/benchmark_llms.py https://stripe.com https://www.notion.so https://example-agency.com
    python scripts/benchmark_llms.py --models gemini:gemini-3.1-flash-lite openai:gpt-6-luna anthropic:claude-haiku-5-5 \
        --runs 2 https://stripe.com

Writes docs/benchmark_results.md and docs/benchmark_results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.services.analysis_service import AnalysisService  # noqa: E402
from app.services.llm_service import LLMError  # noqa: E402
from app.services.website_extractor import WebsiteFetchError  # noqa: E402
from app.utils.cache import TTLCache  # noqa: E402

DEFAULT_MODELS = ["gemini:gemini-3.1-flash-lite", "openai:gpt-6-luna", "anthropic:claude-haiku-5-5"]


async def run(urls: list[str], models: list[str], runs: int) -> list[dict]:
    base = get_settings()
    rows = []
    async with httpx.AsyncClient(timeout=30) as client:
        for spec in models:
            provider, _, model = spec.partition(":")
            if not getattr(base, f"{provider}_api_key", ""):
                print(f"skip {spec}: {provider.upper()}_API_KEY not set")
                continue
            settings = replace(base, llm_provider=provider, llm_model=model, llm_fallback_provider="")
            for url in urls:
                for attempt in range(1, runs + 1):
                    service = AnalysisService(settings, client, TTLCache(0, 0))  # cache disabled
                    started = time.perf_counter()
                    row = {"model": spec, "url": url, "run": attempt}
                    try:
                        result = await service.analyze(url, refresh=True)
                        meta = result.meta
                        row.update(
                            ok=True,
                            llm_latency_s=round(meta.llm_latency_ms / 1000, 2),
                            total_latency_s=round((time.perf_counter() - started), 2),
                            input_tokens=meta.input_tokens,
                            output_tokens=meta.output_tokens,
                            cost_usd=meta.estimated_cost_usd,
                            competitors=len(result.analysis.competitor_analysis.competitors),
                            services_from_site=sum(s.source == "website_content" for s in result.analysis.top_services),
                            warnings=len(meta.warnings),
                        )
                    except (LLMError, WebsiteFetchError, ValueError) as exc:
                        row.update(ok=False, error=getattr(exc, "message", str(exc)))
                    print(json.dumps(row))
                    rows.append(row)
    return rows


def write_report(rows: list[dict]) -> None:
    out = Path(__file__).resolve().parent.parent / "docs"
    out.mkdir(exist_ok=True)
    (out / "benchmark_results.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")

    lines = [
        f"# LLM benchmark (measured {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC})",
        "",
        "| Model | Runs | Success | Median LLM latency (s) | Median total (s) | Avg input tok | Avg output tok "
        "| Avg cost (USD) | Avg competitors | Avg services from site |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model in dict.fromkeys(r["model"] for r in rows):
        group = [r for r in rows if r["model"] == model]
        ok = [r for r in group if r.get("ok")]

        def med(key, ok=ok):
            return f"{statistics.median(r[key] for r in ok):.2f}" if ok else "-"

        def avg(key, fmt="{:.0f}", ok=ok):
            values = [r[key] for r in ok if r.get(key) is not None]
            return fmt.format(sum(values) / len(values)) if values else "-"

        lines.append(
            f"| {model} | {len(group)} | {len(ok)}/{len(group)} | {med('llm_latency_s')} | {med('total_latency_s')} "
            f"| {avg('input_tokens')} | {avg('output_tokens')} | {avg('cost_usd', '{:.5f}')} "
            f"| {avg('competitors', '{:.1f}')} | {avg('services_from_site', '{:.1f}')} |"
        )
    lines += ["", "Websites: " + ", ".join(dict.fromkeys(r["url"] for r in rows))]
    (out / "benchmark_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="provider:model entries")
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    rows = asyncio.run(run(args.urls, args.models, args.runs))
    if rows:
        write_report(rows)


if __name__ == "__main__":
    main()
