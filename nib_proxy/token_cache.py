"""Async cache for the single, shared NiB token used by this proxy.

Since tokens are always requested with ``client=requestip`` (bound to this
proxy's own egress IP, which is the same for every request it makes
upstream regardless of which client/origin is calling it), a single shared
token suffices -- there's no need to partition it per caller. Tokens have a
default validity of 1 hour (configurable). A lock ensures concurrent
requests don't trigger duplicate token fetches, and the cached token can be
force-refreshed on demand (e.g. after an upstream 401/403).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from nib_proxy.config import Settings
from nib_proxy.token_client import fetch_token

logger = logging.getLogger(__name__)


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class TokenCache:
    """In-memory cache for the single shared NiB token."""

    def __init__(self, settings: Settings, safety_margin_seconds: int = 30) -> None:
        self._settings = settings
        self._safety_margin = safety_margin_seconds
        self._entry: _CachedToken | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached token, forcing a refresh next time."""
        if self._entry is not None:
            self._entry = None
            logger.info("Invalidated cached NiB token")

    async def get_token(
        self,
        http_client: httpx.AsyncClient,
        *,
        force_refresh: bool = False,
    ) -> str:
        """Return a valid token, fetching one if needed."""
        if (
            not force_refresh
            and self._entry
            and self._entry.expires_at > time.monotonic()
        ):
            logger.debug("Token cache HIT")
            return self._entry.token

        async with self._lock:
            # Re-check after acquiring the lock: another task may have
            # already refreshed the token while we were waiting.
            if (
                not force_refresh
                and self._entry
                and self._entry.expires_at > time.monotonic()
            ):
                logger.debug("Token cache HIT (after waiting for lock)")
                return self._entry.token

            logger.info(
                "Token cache MISS%s, fetching a new token",
                " (forced refresh)" if force_refresh else "",
            )
            token = await fetch_token(http_client, self._settings)
            expires_at = (
                time.monotonic()
                + self._settings.token_validity_seconds
                - self._safety_margin
            )
            self._entry = _CachedToken(token=token, expires_at=expires_at)
            return token
