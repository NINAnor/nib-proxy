"""Integration-style tests for the proxy app (routing, auth, caching)."""

import httpx
import pytest
import respx
from asgi_lifespan import LifespanManager

from nib_proxy.app import create_app
from nib_proxy.config import CacheConfig, ServiceConfig, Settings


def _settings(*, cache_enabled: bool = False) -> Settings:
    return Settings(
        nib_username="user",
        nib_password="pass",
        token_url="https://services.norgeibilder.no/token/tilecache",
        token_validity_seconds=3600,
        cache_max_entries=100,
        services=(
            ServiceConfig(
                name="wmts-utm32",
                path_prefix="/wmts/utm32",
                upstream="https://tilecache.norgeibilder.no/wmts/utm32_euref89",
                cache=CacheConfig(
                    enabled=cache_enabled, ttl_seconds=60, methods=("GET",)
                ),
            ),
        ),
    )


@pytest.mark.asyncio
@respx.mock
async def test_proxies_request_with_token_header():
    settings = _settings()
    respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )
    upstream = respx.get(
        "https://tilecache.norgeibilder.no/wmts/utm32_euref89/1/2/3.png"
    ).mock(return_value=httpx.Response(200, content=b"tile-bytes"))

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/wmts/utm32/1/2/3.png")

    assert response.status_code == 200
    assert response.content == b"tile-bytes"
    assert upstream.calls.last.request.headers["x-esri-authorization"] == "Bearer tok"


@pytest.mark.asyncio
async def test_returns_404_for_unmatched_path():
    settings = _settings()
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/unknown/path")

    assert response.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_retries_with_fresh_token_on_401():
    settings = _settings()
    respx.post(settings.token_url).mock(
        side_effect=[
            httpx.Response(200, json={"token": "expired"}),
            httpx.Response(200, json={"token": "fresh"}),
        ]
    )
    upstream_route = respx.get(
        "https://tilecache.norgeibilder.no/wmts/utm32_euref89/1/2/3.png"
    ).mock(
        side_effect=[
            httpx.Response(401),
            httpx.Response(200, content=b"tile-bytes"),
        ]
    )

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/wmts/utm32/1/2/3.png")

    assert response.status_code == 200
    assert response.content == b"tile-bytes"
    assert upstream_route.call_count == 2
    last_headers = upstream_route.calls.last.request.headers
    assert last_headers["x-esri-authorization"] == "Bearer fresh"


@pytest.mark.asyncio
@respx.mock
async def test_cached_response_skips_token_and_upstream_on_hit():
    settings = _settings(cache_enabled=True)
    token_route = respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "tok"})
    )
    upstream_route = respx.get(
        "https://tilecache.norgeibilder.no/wmts/utm32_euref89/1/2/3.png"
    ).mock(return_value=httpx.Response(200, content=b"tile-bytes"))

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            first = await client.get("/wmts/utm32/1/2/3.png")
            second = await client.get(
                "/wmts/utm32/1/2/3.png",
                headers={"referer": "https://another-origin.example"},
            )

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"tile-bytes"
    # Only one upstream call and one token call, even though the second
    # request came from a different origin: cache key ignores client identity.
    assert upstream_route.call_count == 1
    assert token_route.call_count == 1


@pytest.mark.asyncio
async def test_healthz():
    settings = _settings()
    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
@respx.mock
async def test_token_endpoint_error_is_propagated_verbatim():
    settings = _settings()
    respx.post(settings.token_url).mock(
        return_value=httpx.Response(
            401, json={"error": "Invalid username or password."}
        )
    )

    app = create_app(settings)
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            response = await client.get("/wmts/utm32/1/2/3.png")

    # The real error from the NiB token endpoint is surfaced as-is (status
    # code and body), instead of a generic/opaque 500, to ease debugging.
    assert response.status_code == 401
    assert response.json() == {"error": "Invalid username or password."}
