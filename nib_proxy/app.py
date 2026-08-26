"""FastAPI application implementing the NiB (Norge i Bilder) proxy.

Incoming requests are matched against the configured service registry
(``services.yaml``), authenticated with a per-origin/IP NiB token (fetched
and cached automatically), and forwarded to the corresponding upstream
service. Responses can optionally be cached (see ``response_cache.py``),
which is especially useful for WMTS tile endpoints.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from nib_proxy.client_key import resolve_client_key
from nib_proxy.config import Settings, load_settings
from nib_proxy.response_cache import ResponseCache
from nib_proxy.token_cache import TokenCache
from nib_proxy.token_client import TokenRequestError

logger = logging.getLogger(__name__)

# Headers that must not be blindly forwarded between proxy <-> upstream.
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _filtered_headers(headers: httpx.Headers | dict) -> dict[str, str]:
    return {
        k: v for k, v in dict(headers).items() if k.lower() not in _HOP_BY_HOP_HEADERS
    }


class _UpstreamTokenError(Exception):
    """Internal signal carrying a token-endpoint error response to forward."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"token endpoint error: {response.status_code}")
        self.response = response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application, wiring up caches and the http client.

    If ``settings.base_path`` is set (e.g. ``/nib``), all routes (including
    ``/healthz``) are mounted under that prefix, so the service can be
    exposed behind a reverse proxy path such as ``https://host/nib/...``
    without the proxy needing to strip the prefix before forwarding.
    """
    settings = settings or load_settings()
    token_cache = TokenCache(settings)
    response_cache = ResponseCache(max_entries=settings.cache_max_entries)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http_client = httpx.AsyncClient(timeout=30.0)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    app = FastAPI(title="NiB Proxy", lifespan=lifespan)
    app.state.settings = settings
    app.state.token_cache = token_cache
    app.state.response_cache = response_cache

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors.allow_origins),
        allow_methods=list(settings.cors.allow_methods),
        allow_headers=list(settings.cors.allow_headers),
        allow_credentials=settings.cors.allow_credentials,
    )

    router = APIRouter(prefix=settings.base_path)

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
    )
    async def proxy(full_path: str, request: Request) -> Response:
        return await handle_proxy_request(app, request, full_path)

    app.include_router(router)

    if settings.base_path:
        # Also expose an unprefixed /healthz, since infra liveness/readiness
        # probes often hit the container directly without knowledge of the
        # externally-visible base path.
        @app.get("/healthz")
        async def healthz_root() -> dict[str, str]:
            return {"status": "ok"}

    return app


async def handle_proxy_request(
    app: FastAPI, request: Request, full_path: str
) -> Response:
    """Resolve, authenticate, cache, and forward a single proxied request."""
    settings: Settings = app.state.settings
    token_cache: TokenCache = app.state.token_cache
    response_cache: ResponseCache = app.state.response_cache
    http_client: httpx.AsyncClient = app.state.http_client

    match = settings.match_service(full_path)
    if match is None:
        return Response(content="No matching service configured", status_code=404)
    service, sub_path = match

    query_string = str(request.url.query)
    cache_enabled = service.cache.enabled and request.method in service.cache.methods
    cache_key = None
    if cache_enabled:
        cache_key = ResponseCache.build_key(
            service.name, request.method, sub_path, query_string
        )
        cached = response_cache.get(cache_key)
        if cached is not None:
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                headers=cached.headers,
            )

    client_key = resolve_client_key(request)
    body = await request.body()

    async def _forward(*, force_refresh: bool) -> httpx.Response:
        try:
            token = await token_cache.get_token(
                http_client, client_key, force_refresh=force_refresh
            )
        except TokenRequestError as exc:
            # Propagate the upstream token endpoint's error as-is (status,
            # headers, body) rather than a generic 500, so failures (bad
            # credentials, rate limits, etc.) are easy to debug directly
            # from the source.
            raise _UpstreamTokenError(exc.response) from exc
        upstream_url = f"{service.upstream}{sub_path}"
        headers = _filtered_headers(request.headers)
        headers["X-Esri-Authorization"] = f"Bearer {token}"
        return await http_client.request(
            request.method,
            upstream_url,
            params=query_string or None,
            content=body or None,
            headers=headers,
        )

    try:
        upstream_response = await _forward(force_refresh=False)

        if upstream_response.status_code in (401, 403):
            token_cache.invalidate(client_key)
            upstream_response = await _forward(force_refresh=True)
    except _UpstreamTokenError as exc:
        return Response(
            content=exc.response.content,
            status_code=exc.response.status_code,
            headers=_filtered_headers(exc.response.headers),
        )

    response_headers = _filtered_headers(upstream_response.headers)

    if cache_enabled and cache_key and upstream_response.status_code < 400:
        response_cache.set(
            cache_key,
            status_code=upstream_response.status_code,
            headers=response_headers,
            body=upstream_response.content,
            ttl_seconds=service.cache.ttl_seconds,
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


app = create_app()
