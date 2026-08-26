"""Tests for the token cache and token client."""

import base64

import httpx
import pytest
import respx

from nib_proxy.client_key import ClientKey
from nib_proxy.config import ServiceConfig, Settings
from nib_proxy.token_cache import TokenCache


def _settings() -> Settings:
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
            ),
        ),
    )


@pytest.mark.asyncio
@respx.mock
async def test_fetch_token_sends_basic_auth_and_form_body():
    settings = _settings()
    route = respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "abc123"})
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token = await cache.get_token(client, ClientKey(mode="ip", value="1.2.3.4"))

    assert token == "abc123"
    request = route.calls.last.request
    expected_auth = "Basic " + base64.b64encode(b"user:pass").decode()
    assert request.headers["authorization"] == expected_auth
    body = request.content.decode()
    assert "client=ip" in body
    assert "ip=1.2.3.4" in body
    assert "expiration=3600" in body


@pytest.mark.asyncio
@respx.mock
async def test_token_is_cached_across_calls():
    settings = _settings()
    route = respx.post(settings.token_url).mock(
        return_value=httpx.Response(200, json={"token": "cached-token"})
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        key = ClientKey(mode="referer", value="example.com")
        token1 = await cache.get_token(client, key)
        token2 = await cache.get_token(client, key)

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
        key = ClientKey(mode="referer", value="example.com")
        token1 = await cache.get_token(client, key)
        token2 = await cache.get_token(client, key, force_refresh=True)

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
        key = ClientKey(mode="referer", value="example.com")
        token1 = await cache.get_token(client, key)
        cache.invalidate(key)
        token2 = await cache.get_token(client, key)

    assert token1 == "first"
    assert token2 == "second"


@pytest.mark.asyncio
@respx.mock
async def test_different_client_keys_get_different_tokens():
    settings = _settings()
    respx.post(settings.token_url).mock(
        side_effect=[
            httpx.Response(200, json={"token": "token-a"}),
            httpx.Response(200, json={"token": "token-b"}),
        ]
    )

    async with httpx.AsyncClient() as client:
        cache = TokenCache(settings)
        token_a = await cache.get_token(client, ClientKey(mode="ip", value="1.1.1.1"))
        token_b = await cache.get_token(client, ClientKey(mode="ip", value="2.2.2.2"))

    assert token_a == "token-a"
    assert token_b == "token-b"
