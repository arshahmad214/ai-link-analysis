"""LLM providers (Gemini, OpenAI, Anthropic) with native structured JSON output.

All three are called over plain HTTPS with httpx (no vendor SDKs), and all three
use their *constrained* structured-output feature so the model must return JSON
matching our schema:

* Gemini    – generateContent with generationConfig.responseFormat (JSON Schema)
* OpenAI    – Responses API with text.format = json_schema, strict = true
* Anthropic – Messages API with a single forced tool whose input_schema is our schema
"""

from __future__ import annotations

import copy
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import Settings

logger = logging.getLogger(__name__)


class LLMError(Exception):
    def __init__(self, message: str, code: str = "llm_error", status_code: int = 502, retryable: bool = True):
        super().__init__(message)
        self.message, self.code, self.status_code, self.retryable = message, code, status_code, retryable


@dataclass
class LLMResult:
    data: dict[str, Any]
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


# ----------------------------------------------------------------------------- schema
def portable_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model into a JSON Schema every provider accepts in strict mode.

    * $ref/$defs are inlined (Gemini does not document $ref support)
    * Optional[X] (anyOf X|null) becomes {"type": [X, "null"]}
    * every object gets additionalProperties=false and lists all properties as required (OpenAI strict)
    * 'title' and 'default' are dropped (not needed, cost tokens)
    """
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def convert(node: Any) -> Any:
        if isinstance(node, list):
            return [convert(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].split("/")[-1]]
            merged = {**copy.deepcopy(target), **{k: v for k, v in node.items() if k != "$ref"}}
            return convert(merged)
        if "anyOf" in node:
            options = node["anyOf"]
            non_null = [o for o in options if o.get("type") != "null"]
            if len(non_null) == 1 and len(options) == 2:
                inner = convert(non_null[0])
                if isinstance(inner.get("type"), str):
                    inner["type"] = [inner["type"], "null"]
                    if "enum" in inner:
                        inner["enum"] = [*inner["enum"], None]
                if node.get("description"):
                    inner["description"] = node["description"]
                return inner
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("title", "default"):  # schema keywords, not property names
                continue
            if key == "properties":
                # A mapping of property NAME -> schema: keep every name (a field may be called "title").
                out[key] = {name: convert(schema) for name, schema in value.items()}
            else:
                out[key] = convert(value)
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out

    return convert(raw)


def _gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return schema  # Gemini's responseFormat accepts this JSON Schema subset as is


# --------------------------------------------------------------------------- providers
class BaseProvider:
    name = ""

    def __init__(self, settings: Settings, client: httpx.AsyncClient, model: str, api_key: str):
        self.settings, self.client, self.model, self.api_key = settings, client, model, api_key

    async def generate(self, system: str, user: str, schema: dict[str, Any]) -> LLMResult:
        if not self.api_key:
            raise LLMError(
                f"{self.name} API key is not configured.", code="llm_not_configured", status_code=503, retryable=False
            )
        started = time.perf_counter()
        try:
            data, input_tokens, output_tokens = await self._call(system, user, schema)
        except httpx.TimeoutException:
            raise LLMError(
                f"{self.name} did not respond within {self.settings.llm_timeout:.0f}s.", "llm_timeout", 504
            ) from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Network error calling {self.name}: {type(exc).__name__}.", "llm_unreachable", 502) from None
        latency = int((time.perf_counter() - started) * 1000)
        logger.info(
            "%s/%s answered in %dms (in=%d, out=%d tokens)", self.name, self.model, latency, input_tokens, output_tokens
        )
        return LLMResult(data, self.name, self.model, input_tokens, output_tokens, latency)

    async def _call(self, system: str, user: str, schema: dict[str, Any]) -> tuple[dict, int, int]:
        raise NotImplementedError

    def _check(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            return body
        error = body.get("error") if isinstance(body, dict) else None
        message = (error.get("message") if isinstance(error, dict) else error) or response.text[:300]
        status = response.status_code
        if status in (401, 403):
            raise LLMError(f"{self.name} rejected the API key ({status}): {message}", "llm_auth_error", 502, retryable=False)
        if status == 429:
            raise LLMError(f"{self.name} rate limit or quota exceeded: {message}", "llm_rate_limited", 503)
        if status == 400:
            raise LLMError(f"{self.name} rejected the request: {message}", "llm_bad_request", 502)
        raise LLMError(f"{self.name} error {status}: {message}", "llm_error", 502)

    @staticmethod
    def _parse_json(text: str, provider: str) -> dict[str, Any]:
        text = (text or "").strip()
        if text.startswith("```"):  # defensive: strip a markdown fence if a model adds one
            text = text.strip("`").removeprefix("json").strip()
        try:
            data = json.loads(text)
        except ValueError:
            raise LLMError(f"{provider} returned invalid JSON.", "llm_invalid_output", 502) from None
        if not isinstance(data, dict):
            raise LLMError(f"{provider} returned JSON that is not an object.", "llm_invalid_output", 502)
        return data


class GeminiProvider(BaseProvider):
    name = "gemini"
    url = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    async def _call(self, system, user, schema):
        generation_config: dict[str, Any] = {
            "maxOutputTokens": self.settings.llm_max_output_tokens,
            "responseFormat": {"text": {"mimeType": "application/json", "schema": _gemini_schema(schema)}},
        }
        if self.settings.gemini_thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.settings.gemini_thinking_level}
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": generation_config,
        }
        headers = {"x-goog-api-key": self.api_key}
        url = self.url.format(model=self.model)
        response = await self.client.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
        if response.status_code == 400 and any(k in response.text.lower() for k in ("responseformat", "response_format")):
            # Older model versions only know the previous field names.
            legacy = dict(generation_config)
            legacy.pop("responseFormat")
            legacy.update({"responseMimeType": "application/json", "responseJsonSchema": _gemini_schema(schema)})
            payload["generationConfig"] = legacy
            response = await self.client.post(url, json=payload, headers=headers, timeout=self.settings.llm_timeout)
        body = self._check(response)

        candidates = body.get("candidates") or []
        if not candidates:
            reason = (body.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise LLMError(f"Gemini returned no answer ({reason}).", "llm_invalid_output", 502)
        candidate = candidates[0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            raise LLMError("Gemini hit the output token limit; raise LLM_MAX_OUTPUT_TOKENS.", "llm_truncated", 502)
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = body.get("usageMetadata") or {}
        output_tokens = int(usage.get("candidatesTokenCount", 0)) + int(usage.get("thoughtsTokenCount", 0))
        return self._parse_json(text, self.name), int(usage.get("promptTokenCount", 0)), output_tokens


class OpenAIProvider(BaseProvider):
    name = "openai"
    url = "https://api.openai.com/v1/responses"

    async def _call(self, system, user, schema):
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": user,
            "max_output_tokens": self.settings.llm_max_output_tokens,
            "text": {"format": {"type": "json_schema", "name": "website_analysis", "schema": schema, "strict": True}},
            "store": False,
        }
        if self.settings.openai_reasoning_effort:
            payload["reasoning"] = {"effort": self.settings.openai_reasoning_effort}
        response = await self.client.post(
            self.url,
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.settings.llm_timeout,
        )
        body = self._check(response)
        if body.get("status") == "incomplete":
            reason = (body.get("incomplete_details") or {}).get("reason", "unknown")
            raise LLMError(f"OpenAI response incomplete ({reason}); raise LLM_MAX_OUTPUT_TOKENS.", "llm_truncated", 502)
        text = ""
        for item in body.get("output") or []:
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if part.get("type") == "refusal":
                    raise LLMError(f"OpenAI refused: {part.get('refusal')}", "llm_refusal", 502, retryable=False)
                if part.get("type") == "output_text":
                    text += part.get("text", "")
        usage = body.get("usage") or {}
        return self._parse_json(text, self.name), int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    url = "https://api.anthropic.com/v1/messages"
    tool_name = "submit_website_analysis"

    async def _call(self, system, user, schema):
        payload = {
            "model": self.model,
            "max_tokens": self.settings.llm_max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [
                {
                    "name": self.tool_name,
                    "description": "Return the complete website analysis.",
                    "input_schema": schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": self.tool_name},
        }
        response = await self.client.post(
            self.url,
            json=payload,
            headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
            timeout=self.settings.llm_timeout,
        )
        body = self._check(response)
        if body.get("stop_reason") == "max_tokens":
            raise LLMError("Anthropic hit the output token limit; raise LLM_MAX_OUTPUT_TOKENS.", "llm_truncated", 502)
        for block in body.get("content") or []:
            if block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                usage = body.get("usage") or {}
                return block["input"], int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        raise LLMError("Anthropic did not return the structured analysis.", "llm_invalid_output", 502)


PROVIDERS: dict[str, tuple[type[BaseProvider], str]] = {
    "gemini": (GeminiProvider, "gemini_api_key"),
    "openai": (OpenAIProvider, "openai_api_key"),
    "anthropic": (AnthropicProvider, "anthropic_api_key"),
}


def build_provider(settings: Settings, client: httpx.AsyncClient, provider: str, model: str = "") -> BaseProvider:
    if provider not in PROVIDERS:
        raise LLMError(
            f"Unknown LLM provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.",
            "llm_not_configured",
            503,
            retryable=False,
        )
    cls, key_attr = PROVIDERS[provider]
    return cls(settings, client, settings.model_for(provider, model), getattr(settings, key_attr))


def estimate_cost(settings: Settings, model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = settings.price_for(model)
    if not price:
        return None
    return round(input_tokens / 1_000_000 * price[0] + output_tokens / 1_000_000 * price[1], 6)
