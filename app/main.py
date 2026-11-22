"""FastAPI entry point: POST /link-analysis."""

from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.config import Settings, get_settings
from app.schemas import ErrorResponse, LinkAnalysisRequest, LinkAnalysisResponse
from app.services.analysis_service import AnalysisService
from app.services.llm_service import LLMError
from app.services.website_extractor import WebsiteFetchError
from app.utils.cache import TTLCache
from app.utils.validators import BlockedURLError, DNSResolutionError, InvalidURLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
# httpx logs every request URL at INFO; keep our own logs readable.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)
    app.state.cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)
    model = settings.model_for(settings.llm_provider, settings.llm_model)
    fallback = ""
    if settings.llm_fallback_provider:
        fallback_model = settings.model_for(settings.llm_fallback_provider, settings.llm_fallback_model)
        fallback = f" (fallback {settings.llm_fallback_provider}/{fallback_model})"
    logger.info(
        "LLM: %s/%s%s | web search: %s",
        settings.llm_provider,
        model,
        fallback,
        "on" if settings.brave_search_api_key else "off",
    )
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(
    title="AI-Powered Website Link Analysis API",
    version=__version__,
    description=(
        "Analyzes a website URL with an LLM and returns five sections: competitor vs brand, top 5 target "
        "search queries, SEO improvements, top 5 services and target audience. The URL is validated and "
        "protected against SSRF; website content is cleaned before it is sent to the LLM; the LLM answers "
        "through a strict JSON schema; results are cached."
    ),
    lifespan=lifespan,
)

_origins = get_settings().cors_origins
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=list(_origins), allow_methods=["GET", "POST"], allow_headers=["*"])


# --------------------------------------------------------------------------- errors
def error_response(status: int, code: str, message: str, website: str | None = None) -> JSONResponse:
    body = ErrorResponse(website=website, error={"code": code, "message": message})
    return JSONResponse(status_code=status, content=body.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or "body"
    kind = first.get("type", "")
    if kind == "missing":
        message = f"'{field}' is required."
    elif kind == "extra_forbidden":
        message = f"Unknown field '{field}'."
    elif kind == "json_invalid":
        message = "Request body is not valid JSON."
    else:
        message = f"{field}: {first.get('msg', 'invalid value')}"
    return error_response(422, "invalid_request", message)


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return error_response(exc.status_code, "http_error", str(exc.detail))


# ----------------------------------------------------------------------- dependencies
def get_http_client(request: Request) -> httpx.AsyncClient:
    # Created in lifespan; created lazily as a fallback for serverless hosts that skip lifespan events.
    if getattr(request.app.state, "http", None) is None:
        request.app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)
    return request.app.state.http


def get_cache(request: Request) -> TTLCache:
    if getattr(request.app.state, "cache", None) is None:
        settings = get_settings()
        request.app.state.cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)
    return request.app.state.cache


def get_analysis_service(
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_http_client),
    cache: TTLCache = Depends(get_cache),
) -> AnalysisService:
    return AnalysisService(settings, client, cache)


def require_api_key(settings: Settings = Depends(get_settings), x_api_key: Optional[str] = Header(default=None)) -> None:
    if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise StarletteHTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


# ------------------------------------------------------------------------------ routes
@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["meta"])
async def health(settings: Settings = Depends(get_settings)) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.model_for(settings.llm_provider, settings.llm_model),
        "llm_configured": bool(getattr(settings, f"{settings.llm_provider}_api_key", "")),
        "web_search": bool(settings.brave_search_api_key),
    }


@app.post(
    "/link-analysis",
    response_model=LinkAnalysisResponse,
    tags=["analysis"],
    summary="Analyze a website with an LLM",
    responses={
        400: {"model": ErrorResponse, "description": "URL points to a private/internal address (SSRF protection)"},
        401: {"model": ErrorResponse},
        422: {"model": ErrorResponse, "description": "Invalid URL, unresolvable domain, page not found or no content"},
        502: {"model": ErrorResponse, "description": "Website or LLM provider error"},
        503: {"model": ErrorResponse, "description": "LLM not configured or rate limited"},
        504: {"model": ErrorResponse, "description": "Website or LLM timeout"},
    },
    dependencies=[Depends(require_api_key)],
)
async def link_analysis(
    payload: LinkAnalysisRequest,
    service: AnalysisService = Depends(get_analysis_service),
    settings: Settings = Depends(get_settings),
):
    try:
        return await service.analyze(payload.url, refresh=payload.refresh)
    except InvalidURLError as exc:
        return error_response(422, "invalid_url", str(exc), payload.url)
    except DNSResolutionError as exc:
        return error_response(422, "dns_error", str(exc), payload.url)
    except BlockedURLError as exc:
        return error_response(400, "blocked_url", str(exc), payload.url)
    except WebsiteFetchError as exc:
        return error_response(exc.status_code, exc.code, exc.message, payload.url)
    except LLMError as exc:
        message = exc.message
        for secret in settings.secret_values():
            message = message.replace(secret, "***")
        return error_response(exc.status_code, exc.code, message, payload.url)
