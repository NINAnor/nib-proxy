"""Resolve the identity ("client key") used to bind/cache NiB tokens.

Per https://www.geonorge.no/nib, a token is bound to either an HTTP Referer
(domain) or an IP address. We prefer the Referer/Origin header when present,
otherwise fall back to the requester's IP address.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import Request


@dataclass(frozen=True)
class ClientKey:
    """Identifies which NiB-token "client" mode and value should be used."""

    mode: str  # "referer" or "ip"
    value: str

    @property
    def cache_key(self) -> str:
        """Key to use for the token cache."""
        return f"{self.mode}:{self.value}"


def _referer_host(request: Request) -> str | None:
    for header in ("referer", "origin"):
        raw = request.headers.get(header)
        if not raw:
            continue
        parsed = urlparse(raw)
        if parsed.netloc:
            return parsed.netloc
    return None


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"


def resolve_client_key(request: Request) -> ClientKey:
    """Resolve the client key for a request: Referer/Origin host, else IP."""
    host = _referer_host(request)
    if host:
        return ClientKey(mode="referer", value=host)
    return ClientKey(mode="ip", value=_client_ip(request))
