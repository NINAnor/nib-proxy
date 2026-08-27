"""FastAPI application implementing the NiB (Norge i Bilder) proxy.

Incoming requests are matched against the configured service registry
(``services.yaml``), authenticated with a per-origin/IP NiB token (fetched
and cached automatically), and forwarded to the corresponding upstream
service. Responses can optionally be cached (see ``response_cache.py``),
which is especially useful for WMTS tile endpoints.
"""

from __future__ import annotations

import logging
import time
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


# Content-type prefixes/suffixes considered textual and safe to log a
# snippet of. Anything else (images, tiles, octet-stream, etc.) is reported
# as binary with just its size.
_TEXTUAL_CONTENT_TYPE_PREFIXES = ("text/",)
_TEXTUAL_CONTENT_TYPE_EXACT = {
    "application/json",
    "application/xml",
    "application/problem+json",
    "application/x-www-form-urlencoded",
}
_TEXTUAL_CONTENT_TYPE_SUFFIXES = ("+json", "+xml")

_BODY_LOG_PREVIEW_LIMIT = 500


def _describe_body(headers: httpx.Headers | dict, body: bytes) -> str:
    """Summarize a response body for logging.

    Returns a short text preview if the content-type looks textual, or a
    ``<binary, N bytes>`` indicator otherwise, so we never dump raw tile
    images/binary payloads into the logs.
    """
    if not body:
        return "<empty body>"

    content_type = dict(headers).get("content-type", "").split(";")[0].strip().lower()
    is_textual = (
        content_type.startswith(_TEXTUAL_CONTENT_TYPE_PREFIXES)
        or content_type in _TEXTUAL_CONTENT_TYPE_EXACT
        or content_type.endswith(_TEXTUAL_CONTENT_TYPE_SUFFIXES)
    )

    if not is_textual:
        return f"<binary, {len(body)} bytes, content-type={content_type or 'unknown'}>"

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary, {len(body)} bytes, content-type={content_type or 'unknown'}>"

    if len(text) > _BODY_LOG_PREVIEW_LIMIT:
        return (
            f"{text[:_BODY_LOG_PREVIEW_LIMIT]}... (truncated, {len(body)} bytes total)"
        )
    return text


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

    logger.info(
        "Loaded %d configured service(s): %s",
        len(settings.services),
        ", ".join(s.name for s in settings.services) or "none",
    )
    if settings.base_path:
        logger.info("Serving under base path %r", settings.base_path)
    if not settings.nib_username or not settings.nib_password:
        logger.warning(
            "NIB_USERNAME/NIB_PASSWORD are not set; requests to the token "
            "endpoint will fail authentication."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.http_client = httpx.AsyncClient(timeout=30.0)
        logger.info("NiB proxy startup complete")
        try:
            yield
        finally:
            await app.state.http_client.aclose()
            logger.info("NiB proxy shutdown complete")

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

    @router.get("/services")
    async def list_services() -> list[dict]:
        """List the configured upstream services (introspection/debugging)."""
        return [
            {
                "name": service.name,
                "path_prefix": f"{settings.base_path}{service.path_prefix}",
                "upstream": service.upstream,
                "cache": {
                    "enabled": service.cache.enabled,
                    "ttl_seconds": service.cache.ttl_seconds,
                    "methods": list(service.cache.methods),
                },
            }
            for service in settings.services
        ]

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

    start = time.monotonic()
    method = request.method

    match = settings.match_service(full_path)
    if match is None:
        logger.warning("No service matches path %r (method=%s)", full_path, method)
        return Response(content="No matching service configured", status_code=404)
    service, sub_path = match

    query_string = str(request.url.query)
    logger.info(
        "Request %s /%s -> service=%s sub_path=%r",
        method,
        full_path,
        service.name,
        sub_path,
    )

    cache_enabled = service.cache.enabled and method in service.cache.methods
    cache_key = None
    if cache_enabled:
        cache_key = ResponseCache.build_key(
            service.name, method, sub_path, query_string
        )
        cached = response_cache.get(cache_key)
        if cached is not None:
            logger.info(
                "Cache HIT for %s (key=%r), skipping token+upstream call",
                service.name,
                cache_key,
            )
            return Response(
                content=cached.body,
                status_code=cached.status_code,
                headers=cached.headers,
            )
        logger.debug("Cache MISS for %s (key=%r)", service.name, cache_key)

    client_key = resolve_client_key(request)
    logger.debug(
        "Resolved client key mode=%s value=%s for %s",
        client_key.mode,
        client_key.value,
        service.name,
    )
    body = await request.body()

    async def _forward(*, force_refresh: bool) -> httpx.Response:
        try:
            token = await token_cache.get_token(
                http_client, client_key, force_refresh=force_refresh
            )
        except TokenRequestError as exc:
            logger.error(
                "Token acquisition failed for %s (client=%s): %s",
                service.name,
                client_key.cache_key,
                exc,
            )
            # Propagate the upstream token endpoint's error as-is (status,
            # headers, body) rather than a generic 500, so failures (bad
            # credentials, rate limits, etc.) are easy to debug directly
            # from the source.
            raise _UpstreamTokenError(exc.response) from exc
        upstream_url = f"{service.upstream}{sub_path}"
        headers = _filtered_headers(request.headers)
        forward_params = httpx.QueryParams(query_string).set("token", token)
        logger.debug(
            "Forwarding %s %s?%s to upstream %s using token=%s",
            method,
            upstream_url,
            forward_params,
            service.upstream,
            token,
        )
        return await http_client.request(
            method,
            upstream_url,
            params=forward_params,
            content=body or None,
            headers=headers,
        )

    try:
        upstream_response = await _forward(force_refresh=False)

        if upstream_response.status_code in (401, 403):
            logger.warning(
                "Upstream %s returned %d for %s (client=%s); "
                "invalidating token and retrying once",
                service.name,
                upstream_response.status_code,
                full_path,
                client_key.cache_key,
            )
            token_cache.invalidate(client_key)
            upstream_response = await _forward(force_refresh=True)
    except _UpstreamTokenError as exc:
        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Request %s /%s failed at token stage: %d (%.1f ms)",
            method,
            full_path,
            exc.response.status_code,
            elapsed_ms,
        )
        logger.debug(
            "Token endpoint response body: %s",
            _describe_body(exc.response.headers, exc.response.content),
        )
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
        logger.debug(
            "Cached response for %s (key=%r, ttl=%ds)",
            service.name,
            cache_key,
            service.cache.ttl_seconds,
        )

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "Request %s /%s -> %d (%.1f ms, service=%s)",
        method,
        full_path,
        upstream_response.status_code,
        elapsed_ms,
        service.name,
    )
    logger.debug(
        "Response body: %s",
        _describe_body(response_headers, upstream_response.content),
    )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


app = create_app()
