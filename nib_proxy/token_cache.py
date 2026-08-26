"""Async cache for NiB tokens, keyed by client key.

Tokens have a default validity of 1 hour (configurable). A per-key lock
ensures concurrent requests from the same origin/IP don't trigger duplicate
token fetches, and expired/invalid tokens can be force-refreshed on demand
(e.g. after an upstream 401/403).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from nib_proxy.client_key import ClientKey
from nib_proxy.config import Settings
from nib_proxy.token_client import fetch_token


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class TokenCache:
    """In-memory token cache with per-key locking and forced refresh."""

    def __init__(self, settings: Settings, safety_margin_seconds: int = 30) -> None:
        self._settings = settings
        self._safety_margin = safety_margin_seconds
        self._entries: dict[str, _CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def invalidate(self, client_key: ClientKey) -> None:
        """Drop the cached token for a client key, forcing a refresh next time."""
        self._entries.pop(client_key.cache_key, None)

    async def get_token(
        self,
        http_client: httpx.AsyncClient,
        client_key: ClientKey,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Return a valid token for the client key, fetching one if needed."""
        key = client_key.cache_key

        if not force_refresh:
            cached = self._entries.get(key)
            if cached and cached.expires_at > time.monotonic():
                return cached.token

        async with self._lock_for(key):
            # Re-check after acquiring the lock: another task may have
            # already refreshed the token while we were waiting.
            if not force_refresh:
                cached = self._entries.get(key)
                if cached and cached.expires_at > time.monotonic():
                    return cached.token

            token = await fetch_token(http_client, self._settings, client_key)
            expires_at = (
                time.monotonic()
                + self._settings.token_validity_seconds
                - self._safety_margin
            )
            self._entries[key] = _CachedToken(token=token, expires_at=expires_at)
            return token
