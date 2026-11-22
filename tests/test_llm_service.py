"""Each provider against a fake version of its REST API."""

import asyncio
import json

import httpx
import pytest

from app.schemas import LLMAnalysis
from app.services.llm_service import (
    LLMError,
    build_provider,
    estimate_cost,
    portable_json_schema,
)

from .conftest import make_settings, valid_llm_output

SCHEMA = portable_json_schema(LLMAnalysis)


def walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def test_schema_is_strict_and_portable():
    text = json.dumps(SCHEMA)
    assert "$ref" not in text and "$defs" not in text and "anyOf" not in text
    # the 'title' keyword (a string) is dropped; only a property called "title" (a schema dict) may remain
    assert not any(isinstance(node.get("title"), str) for node in walk(SCHEMA))
    for node in walk(SCHEMA):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
    website = SCHEMA["properties"]["competitor_analysis"]["properties"]["competitors"]["items"]["properties"]["website"]
    assert website["type"] == ["string", "null"]
    # a field literally named "title" must survive the removal of the 'title' keyword
    content_item = SCHEMA["properties"]["seo_improvements"]["properties"]["supporting_content"]["items"]
    assert "title" in content_item["properties"] and "title" in content_item["required"]


def test_every_pydantic_field_is_in_the_schema():
    """The schema sent to the LLM must describe exactly the fields Pydantic will require."""
    from pydantic import BaseModel

    def check(model: type[BaseModel], schema: dict):
        assert set(schema["properties"]) == set(model.model_fields), model.__name__
        for name, field in model.model_fields.items():
            sub = schema["properties"][name]
            ann = field.annotation
            inner = getattr(ann, "__args__", [None])[0]
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                check(ann, sub)
            elif isinstance(inner, type) and issubclass(inner, BaseModel):
                check(inner, sub["items"])

    check(LLMAnalysis, SCHEMA)


def test_schema_accepts_valid_output():
    assert LLMAnalysis.model_validate(valid_llm_output()).seo_improvements.supporting_content[0].title


def provider_with(handler, provider, **settings):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = make_settings(openai_api_key="sk-test", anthropic_api_key="ak-test", **settings)
    return build_provider(s, client, provider)


def test_gemini_request_and_response():
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers["x-goog-api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(valid_llm_output())}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5000, "candidatesTokenCount": 2000, "thoughtsTokenCount": 100},
        })  # fmt: skip

    result = asyncio.run(provider_with(handler, "gemini").generate("sys", "user", SCHEMA))
    assert "models/gemini-3.1-flash-lite:generateContent" in seen["url"]
    assert seen["key"] == "test-gemini-key"
    assert seen["body"]["generationConfig"]["responseFormat"]["text"]["mimeType"] == "application/json"
    assert seen["body"]["systemInstruction"]["parts"][0]["text"] == "sys"
    assert result.data["brand"]["name"] == "Brightpath Digital"
    assert (result.input_tokens, result.output_tokens) == (5000, 2100)  # thinking tokens are billed as output


def test_gemini_falls_back_to_legacy_fields():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["generationConfig"])
        if "responseFormat" in body["generationConfig"]:
            return httpx.Response(400, json={"error": {"message": 'Unknown name "responseFormat"'}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}})

    asyncio.run(provider_with(handler, "gemini").generate("s", "u", SCHEMA))
    assert calls[1]["responseMimeType"] == "application/json" and "responseJsonSchema" in calls[1]


def test_gemini_max_tokens_is_an_error():
    def handler(request):
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"text": "{"}]}, "finishReason": "MAX_TOKENS"}]}
        )

    with pytest.raises(LLMError) as exc_info:
        asyncio.run(provider_with(handler, "gemini").generate("s", "u", SCHEMA))
    assert exc_info.value.code == "llm_truncated"


def test_openai_responses_api():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(valid_llm_output())}]},
            ],
            "usage": {"input_tokens": 4000, "output_tokens": 2500},
        })  # fmt: skip

    result = asyncio.run(provider_with(handler, "openai").generate("sys", "user", SCHEMA))
    fmt = seen["body"]["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True
    assert seen["body"]["model"] == "gpt-6-luna" and seen["body"]["reasoning"] == {"effort": "low"}
    assert seen["auth"] == "Bearer sk-test"
    assert result.output_tokens == 2500 and result.data["brand"]["category"] == "Web Development"


def test_openai_refusal():
    def handler(request):
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]})  # fmt: skip

    with pytest.raises(LLMError, match="refused"):
        asyncio.run(provider_with(handler, "openai").generate("s", "u", SCHEMA))


def test_anthropic_forced_tool():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, json={
            "content": [{"type": "tool_use", "name": "submit_website_analysis", "input": valid_llm_output()}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 4200, "output_tokens": 2300},
        })  # fmt: skip

    result = asyncio.run(provider_with(handler, "anthropic").generate("sys", "user", SCHEMA))
    assert seen["body"]["tool_choice"] == {"type": "tool", "name": "submit_website_analysis"}
    assert seen["body"]["model"] == "claude-haiku-5-5"
    assert seen["headers"]["x-api-key"] == "ak-test" and seen["headers"]["anthropic-version"] == "2023-06-01"
    assert result.data["brand"]["name"] == "Brightpath Digital"


@pytest.mark.parametrize(
    "status, code",
    [(401, "llm_auth_error"), (429, "llm_rate_limited"), (400, "llm_bad_request"), (500, "llm_error")],
)
def test_http_errors_are_mapped(status, code):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "boom"}})

    with pytest.raises(LLMError) as exc_info:
        asyncio.run(provider_with(handler, "openai").generate("s", "u", SCHEMA))
    assert exc_info.value.code == code


def test_missing_key_is_reported():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    provider = build_provider(make_settings(gemini_api_key=""), client, "gemini")
    with pytest.raises(LLMError) as exc_info:
        asyncio.run(provider.generate("s", "u", SCHEMA))
    assert exc_info.value.code == "llm_not_configured" and exc_info.value.status_code == 503


def test_cost_estimate_uses_price_table():
    settings = make_settings()
    # 6,000 input + 2,500 output tokens on gemini-3.1-flash-lite ($0.25 / $1.50 per 1M)
    assert estimate_cost(settings, "gemini-3.1-flash-lite", 6000, 2500) == pytest.approx(0.00525)
    assert estimate_cost(settings, "unknown-model", 1, 1) is None
    custom = make_settings(price_input_per_m=1.0, price_output_per_m=2.0)
    assert estimate_cost(custom, "unknown-model", 1_000_000, 1_000_000) == 3.0


def test_schema_is_valid_json_schema_and_matches_fixture():
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    validator = jsonschema.Draft202012Validator(SCHEMA)
    assert not list(validator.iter_errors(valid_llm_output()))
    bad = valid_llm_output()
    bad["target_search_queries"][0]["platforms"] = ["TikTok"]
    assert list(validator.iter_errors(bad))
