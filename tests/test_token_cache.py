"""Tests for the token cache and token client."""

import base64

import httpx
import pytest
import respx

from nib_proxy.config import ServiceConfig, Settings
from nib_proxy.token_cache import TokenCache


def _settings() -> Settings:
    return Settings(
        nib_username="user",
        nib_password="pass",
        token_url="https://backend-api.klienter-prod-k8s2.norgeibilder.no/token/tilecache",
        token_validity_seconds=3600,
        services=(
            ServiceConfig(
                name="wmts-utm32",
                path_prefix="/wmts/utm32",
                upstream="https://tilecache.norgeibilder.no/wmts/utm32_euref89",
            ),
        ),
    )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_sends_basic_auth_and_requestip_form_body():
    settings = _settings()
    route = respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token = await cache.get_token(client)

    assert token == "abc123"
    request = route.calls.last.request
    expected_auth = "Basic " + base64.b64encode(b"user:pass").decode()
    assert request.headers["authorization"] == expected_auth
    body = request.content.decode()
    assert "client=requestip" in body
    assert "expiration=3600" in body
    assert "ip=" not in body
    assert "referer=" not in body


@pytest.mark.asyncio
@respx.mock
async def test_token_is_cached_across_calls():
    settings = _settings()
    route = respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "cached-token"})
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token1 = await cache.get_token(client)
        token2 = await cache.get_token(client)

    assert token1 == token2 == "cached-token"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_force_refresh_fetches_new_token():
    settings = _settings()
    respx.post(settings.token_url).mock(
        side_effect=[
            httpx.Response(200, json={"token": "first"}),
            httpx.Response(200, json={"token": "second"}),
        ]
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token1 = await cache.get_token(client)
        token2 = await cache.get_token(client, force_refresh=True)

    assert token1 == "first"
    assert token2 == "second"


@pytest.mark.asyncio
@respx.mock
async def test_invalidate_forces_new_token_fetch():
    settings = _settings()
    respx.post(settings.token_url).mock(
        side_effect=[
            httpx.Response(200, json={"token": "first"}),
            httpx.Response(200, json={"token": "second"}),
        ]
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token1 = await cache.get_token(client)
        cache.invalidate()
        token2 = await cache.get_token(client)

    assert token1 == "first"
    assert token2 == "second"
