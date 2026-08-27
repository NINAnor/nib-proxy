"""In-memory LRU+TTL cache for proxied upstream responses.

Especially useful for WMTS tile endpoints, where the same tile is requested
repeatedly by many different clients/origins. The cache key intentionally
excludes the client key (Referer/IP) since the response bytes don't depend
on which token was used to fetch them.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    expires_at: float


class ResponseCache:
    """A simple LRU cache with per-entry TTL expiry."""

    def __init__(self, max_entries: int) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, CachedResponse] = OrderedDict()

    @staticmethod
    def build_key(service_name: str, method: str, path: str, query_string: str) -> str:
        """Build a cache key from the request, ignoring the caller's identity."""
        return f"{service_name}:{method}:{path}?{query_string}"

    def get(self, key: str) -> CachedResponse | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del self._entries[key]
            logger.debug("Response cache entry expired for key=%r", key)
            return None
        # Mark as recently used.
        self._entries.move_to_end(key)
        return entry

    def set(
        self,
        key: str,
        *,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        ttl_seconds: int,
    ) -> None:
        self._entries[key] = CachedResponse(
            status_code=status_code,
            headers=headers,
            body=body,
            expires_at=time.monotonic() + ttl_seconds,
        )
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            evicted_key, _ = self._entries.popitem(last=False)
            logger.debug(
                "Response cache evicted LRU entry key=%r (max_entries=%d)",
                evicted_key,
                self._max_entries,
            )

    def __len__(self) -> int:
        return len(self._entries)
