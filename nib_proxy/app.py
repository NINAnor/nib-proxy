"""FastAPI application implementing the NiB (Norge i Bilder) proxy.

Incoming requests are matched against the configured service registry
(``services.yaml``), authenticated with a single shared NiB token (fetched
and cached automatically, bound to this proxy's own request IP), and
forwarded to the corresponding upstream service.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cache
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from nib_proxy.config import ServiceConfig, Settings, load_settings
from nib_proxy.token_cache import TokenCache
from nib_proxy.token_client import TokenRequestError

logger = logging.getLogger(__name__)

# httpx transparently decompresses responses (based on Content-Encoding), so
# `response.content` is already decoded. If we forwarded the original
# Content-Encoding/Content-Length headers as-is, the client would be told
# the body is still gzip-compressed at its original length, corrupting the
# response. These must be dropped/recomputed, not forwarded verbatim.
_RESPONSE_HEADERS_TO_DROP = {"content-encoding", "content-length"}


def _response_headers(headers: httpx.Headers | dict) -> dict[str, str]:
    return {
        k: v
        for k, v in dict(headers).items()
        if k.lower() not in _RESPONSE_HEADERS_TO_DROP
    }


# Content-type prefixes/suffixes considered textual: safe to log a preview
# of, and eligible for upstream-URL rewriting. Anything else (images,
# tiles, octet-stream, etc.) is left untouched.
_TEXTUAL_CONTENT_TYPE_PREFIXES = ("text/",)
_TEXTUAL_CONTENT_TYPE_EXACT = {
    "application/json",
    "application/xml",
    "application/problem+json",
    "application/x-www-form-urlencoded",
}
_TEXTUAL_CONTENT_TYPE_SUFFIXES = ("+json", "+xml")

_BODY_LOG_PREVIEW_LIMIT = 500


def _is_textual_content_type(content_type: str) -> bool:
    content_type = content_type.split(";")[0].strip().lower()
    return (
        content_type.startswith(_TEXTUAL_CONTENT_TYPE_PREFIXES)
        or content_type in _TEXTUAL_CONTENT_TYPE_EXACT
        or content_type.endswith(_TEXTUAL_CONTENT_TYPE_SUFFIXES)
    )


def _describe_body(headers: httpx.Headers | dict, body: bytes) -> str:
    """Summarize a response body for logging.

    Returns a short text preview if the content-type looks textual, or a
    ``<binary, N bytes>`` indicator otherwise, so we never dump raw tile
    images/binary payloads into the logs.
    """
    if not body:
        return "<empty body>"

    content_type = dict(headers).get("content-type", "")
    if not _is_textual_content_type(content_type):
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


@cache
def _upstream_url_pattern(upstream: str) -> re.Pattern[str]:
    """Build a pattern matching any scheme/port variant of an upstream URL.

    Response bodies (WMS/WMTS Capabilities documents) may embed the
    upstream's host with a different scheme (http/https) or an explicit
    port than what's configured in ``services.yaml``, so we match on
    host+path only, allowing any scheme and optional port.
    """
    parsed = urlsplit(upstream)
    host = re.escape(parsed.hostname or "")
    path = re.escape(parsed.path)
    return re.compile(rf"https?://{host}(?::\d+)?{path}")


def _rewrite_upstream_urls(
    body: bytes,
    headers: httpx.Headers | dict,
    settings: Settings,
    service: ServiceConfig,
) -> bytes:
    """Rewrite occurrences of the upstream's URL in textual bodies.

    WMS/WMTS Capabilities documents embed the upstream's own base URL for
    subsequent requests (e.g. GetTile/GetMap), sometimes with a different
    scheme or an explicit port. If left untouched, clients would be
    pointed directly at the upstream, bypassing this proxy (and its
    authentication) entirely. Only applied when ``PUBLIC_BASE_URL`` is
    configured, and only to textual bodies.
    """
    if not settings.public_base_url or not body:
        return body

    content_type = dict(headers).get("content-type", "")
    if not _is_textual_content_type(content_type):
        return body

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    external_url = settings.external_url_for(service)
    rewritten, count = _upstream_url_pattern(service.upstream).subn(external_url, text)
    if count == 0:
        return body

    logger.debug(
        "Rewrote %d occurrence(s) of upstream URL for %s -> %s",
        count,
        service.name,
        external_url,
    )
    return rewritten.encode("utf-8")


class _UpstreamTokenError(Exception):
    """Internal signal carrying a token-endpoint error response to forward."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"token endpoint error: {response.status_code}")
        self.response = response


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the FastAPI application, wiring up the token cache and http client.

    If ``settings.base_path`` is set (e.g. ``/nib``), all routes (including
    ``/healthz``) are mounted under that prefix, so the service can be
    exposed behind a reverse proxy path such as ``https://host/nib/...``
    without the proxy needing to strip the prefix before forwarding.
    """
    settings = settings or load_settings()
    token_cache = TokenCache(settings)

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
    """Resolve, authenticate, and forward a single proxied request."""
    settings: Settings = app.state.settings
    token_cache: TokenCache = app.state.token_cache
    http_client: httpx.AsyncClient = app.state.http_client

    start = time.monotonic()
    method = request.method

    match = settings.match_service(full_path)
    if match is None:
        logger.warning("No service matches path %r (method=%s)", full_path, method)
        return Response(content="No matching service configured", status_code=404)
    service, sub_path = match.service, match.sub_path

    query_string = str(request.url.query)
    logger.info(
        "Request %s /%s -> service=%s sub_path=%r",
        method,
        full_path,
        service.name,
        sub_path,
    )

    body = await request.body()

    async def _forward(*, force_refresh: bool) -> httpx.Response:
        try:
            token = await token_cache.get_token(
                http_client, force_refresh=force_refresh
            )
        except TokenRequestError as exc:
            logger.error(
                "Token acquisition failed for %s: %s",
                service.name,
                exc,
            )
            # Propagate the upstream token endpoint's error as-is (status,
            # headers, body) rather than a generic 500, so failures (bad
            # credentials, rate limits, etc.) are easy to debug directly
            # from the source.
            raise _UpstreamTokenError(exc.response) from exc
        upstream_url = f"{service.upstream}{sub_path}"
        headers = dict(request.headers)
        # The inbound Host header refers to this proxy, not the upstream
        # service; forwarding it verbatim breaks virtual-host routing on
        # the upstream's end (it returns a generic 404). Let httpx set the
        # correct Host for the upstream URL instead.
        headers.pop("host", None)
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
                "Upstream %s returned %d for %s; invalidating token and "
                "retrying once",
                service.name,
                upstream_response.status_code,
                full_path,
            )
            token_cache.invalidate()
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
            headers=_response_headers(exc.response.headers),
        )

    response_headers = _response_headers(upstream_response.headers)
    response_body = _rewrite_upstream_urls(
        upstream_response.content, response_headers, settings, service
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
        _describe_body(response_headers, response_body),
    )

    return Response(
        content=response_body,
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


app = create_app()
