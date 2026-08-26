"""Configuration for the NiB proxy.

Reads credentials and general settings from environment variables, and the
service registry from a YAML/JSON config file so that new upstream services
can be added without touching the code.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

import environ
import yaml

env = environ.Env()
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
environ.Env.read_env(str(BASE_DIR / ".env"))


@dataclass(frozen=True)
class CacheConfig:
    """Response cache settings for a single service."""

    enabled: bool = False
    ttl_seconds: int = 300
    methods: tuple[str, ...] = ("GET",)


@dataclass(frozen=True)
class ServiceConfig:
    """A single proxied upstream service."""

    name: str
    path_prefix: str
    upstream: str
    cache: CacheConfig = field(default_factory=CacheConfig)

    def __post_init__(self) -> None:
        """Normalize prefix/upstream so they don't end with a trailing slash."""
        object.__setattr__(self, "path_prefix", self.path_prefix.rstrip("/"))
        object.__setattr__(self, "upstream", self.upstream.rstrip("/"))


@dataclass(frozen=True)
class CorsConfig:
    """CORS settings for the proxy.

    Since this proxy is meant to be called directly from browsers on
    arbitrary origins (the same origins that get bound to NiB tokens via the
    Referer header), CORS must be handled explicitly rather than left to
    fail silently in the browser.
    """

    allow_origins: tuple[str, ...] = ("*",)
    allow_methods: tuple[str, ...] = ("*",)
    allow_headers: tuple[str, ...] = ("*",)
    allow_credentials: bool = False


@dataclass(frozen=True)
class Settings:
    """Global proxy settings."""

    nib_username: str
    nib_password: str
    token_url: str
    token_validity_seconds: int
    cache_max_entries: int
    services: tuple[ServiceConfig, ...]
    cors: CorsConfig = field(default_factory=CorsConfig)

    def match_service(self, path: str) -> tuple[ServiceConfig, str] | None:
        """Find the service whose prefix matches the given path.

        Returns a tuple of (service, remaining sub-path) or None if no
        service matches. The longest matching prefix wins.
        """
        path = "/" + path.lstrip("/")
        best: ServiceConfig | None = None
        for service in self.services:
            prefix = service.path_prefix
            if path == prefix or path.startswith(prefix + "/"):
                if best is None or len(service.path_prefix) > len(best.path_prefix):
                    best = service
        if best is None:
            return None
        sub_path = path[len(best.path_prefix) :]
        return best, sub_path


def _load_services(config_path: pathlib.Path) -> tuple[ServiceConfig, ...]:
    if not config_path.exists():
        return ()

    text = config_path.read_text()
    if config_path.suffix in (".yaml", ".yml"):
        raw = yaml.safe_load(text) or []
    else:
        raw = json.loads(text or "[]")

    services = []
    for entry in raw:
        cache_raw = entry.get("cache") or {}
        cache = CacheConfig(
            enabled=bool(cache_raw.get("enabled", False)),
            ttl_seconds=int(cache_raw.get("ttl_seconds", 300)),
            methods=tuple(m.upper() for m in cache_raw.get("methods", ["GET"])),
        )
        services.append(
            ServiceConfig(
                name=entry["name"],
                path_prefix=entry["path_prefix"],
                upstream=entry["upstream"],
                cache=cache,
            )
        )
    return tuple(services)


def load_settings() -> Settings:
    """Load settings from environment variables and the services config file."""
    config_path = pathlib.Path(
        env.str("SERVICES_CONFIG_PATH", default=str(BASE_DIR / "services.yaml"))
    )
    return Settings(
        nib_username=env.str("NIB_USERNAME", default=""),
        nib_password=env.str("NIB_PASSWORD", default=""),
        token_url=env.str(
            "NIB_TOKEN_URL",
            default="https://services.norgeibilder.no/token/tilecache",
        ),
        token_validity_seconds=env.int("TOKEN_VALIDITY_SECONDS", default=3600),
        cache_max_entries=env.int("CACHE_MAX_ENTRIES", default=5000),
        services=_load_services(config_path),
        cors=CorsConfig(
            allow_origins=tuple(env.list("CORS_ALLOW_ORIGINS", default=["*"])),
            allow_methods=tuple(env.list("CORS_ALLOW_METHODS", default=["*"])),
            allow_headers=tuple(env.list("CORS_ALLOW_HEADERS", default=["*"])),
            allow_credentials=env.bool("CORS_ALLOW_CREDENTIALS", default=False),
        ),
    )
