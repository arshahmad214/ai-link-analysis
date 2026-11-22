"""HTTP layer: status codes and error format (website and LLM are faked)."""

import httpx
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import app, get_cache, get_http_client  # noqa: E402
from app.services import analysis_service  # noqa: E402
from app.services.llm_service import LLMResult  # noqa: E402
from app.utils.cache import TTLCache  # noqa: E402

from .conftest import make_settings, site_handler, valid_llm_output  # noqa: E402


class OkProvider:
    async def generate(self, system, user, schema):
        return LLMResult(valid_llm_output(), "gemini", "gemini-3.1-flash-lite", 6000, 2500, 900)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(analysis_service, "build_provider", lambda *a, **k: OkProvider())
    http = httpx.AsyncClient(transport=httpx.MockTransport(site_handler))
    cache = TTLCache(3600, 10)
    app.dependency_overrides[get_settings] = lambda: make_settings()
    app.dependency_overrides[get_http_client] = lambda: http
    app.dependency_overrides[get_cache] = lambda: cache
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_success_matches_expected_structure(client):
    response = client.post("/link-analysis", json={"url": "https://brightpath.example.com"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True and body["website"] == "https://brightpath.example.com/"
    assert set(body["brand"]) == {"name", "category", "summary"}
    assert set(body["analysis"]) >= {
        "competitor_analysis",
        "target_search_queries",
        "seo_improvements",
        "top_services",
        "target_audience",
    }
    assert body["meta"]["estimated_cost_usd"] > 0


@pytest.mark.parametrize(
    "payload, status, code",
    [
        ({"url": "ftp://x.com"}, 422, "invalid_url"),
        ({"url": "http://127.0.0.1/admin"}, 400, "blocked_url"),
        ({"url": "http://internal.example.com"}, 400, "blocked_url"),
        ({"url": "https://nope.example.org"}, 422, "dns_error"),
        ({"url": "https://brightpath.example.com/forbidden"}, 502, "website_access_denied"),
        ({}, 422, "invalid_request"),
        ({"url": "https://brightpath.example.com", "model": "x"}, 422, "invalid_request"),
    ],
)
def test_errors(client, payload, status, code):
    response = client.post("/link-analysis", json=payload)
    assert response.status_code == status
    body = response.json()
    assert body["success"] is False and body["error"]["code"] == code


def test_api_key(client):
    app.dependency_overrides[get_settings] = lambda: make_settings(api_key="secret-123")
    assert client.post("/link-analysis", json={"url": "brightpath.example.com"}).status_code == 401
    ok = client.post("/link-analysis", json={"url": "brightpath.example.com"}, headers={"X-API-Key": "secret-123"})
    assert ok.status_code == 200


def test_health_and_docs(client):
    assert client.get("/health").json()["llm_configured"] is True
    assert client.get("/docs").status_code == 200
