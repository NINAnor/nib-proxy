"""Configuration for the NiB proxy.

Reads credentials and general settings from environment variables, and the
service registry from a YAML/JSON config file so that new upstream services
can be added without touching the code.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import environ
import yaml

env = environ.Env()
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
environ.Env.read_env(str(BASE_DIR / ".env"))

# Internal proxy path segment used for "passthrough" requests: absolute
# upstream URLs (e.g. ArcGIS's own canonical REST paths embedded in
# Capabilities documents, which don't share the friendly alias path
# configured for a service) are rewritten to
# ``{public_base_url}{base_path}/_upstream/{service.name}{original_path}``
# so that following them still routes back through this proxy.
PASSTHROUGH_SEGMENT = "_upstream"


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
    origin: str = field(init=False)

    def __post_init__(self) -> None:
        """Normalize prefix/upstream so they don't end with a trailing slash."""
        object.__setattr__(self, "path_prefix", self.path_prefix.rstrip("/"))
        object.__setattr__(self, "upstream", self.upstream.rstrip("/"))
        parsed = urlsplit(self.upstream)
        object.__setattr__(self, "origin", f"{parsed.scheme}://{parsed.netloc}")


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
class RouteMatch:
    """Result of matching an inbound path against the service registry."""

    service: ServiceConfig
    sub_path: str
    passthrough: bool = False


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
    base_path: str = ""
    public_base_url: str = ""

    def __post_init__(self) -> None:
        """Normalize base_path: no trailing slash, leading slash if set."""
        base_path = self.base_path.strip().rstrip("/")
        if base_path and not base_path.startswith("/"):
            base_path = "/" + base_path
        object.__setattr__(self, "base_path", base_path)
        object.__setattr__(self, "public_base_url", self.public_base_url.rstrip("/"))

    def match_service(self, path: str) -> RouteMatch | None:
        """Find the service whose prefix matches the given path.

        Checks friendly alias prefixes first (longest match wins). If none
        match, also checks the internal passthrough prefix
        (``/_upstream/<service-name>/...``) used for absolute upstream URLs
        rewritten into response bodies (see ``external_passthrough_url_for``).
        Returns ``None`` if nothing matches.
        """
        path = "/" + path.lstrip("/")

        best: ServiceConfig | None = None
        for service in self.services:
            prefix = service.path_prefix
            if path == prefix or path.startswith(prefix + "/"):
                if best is None or len(service.path_prefix) > len(best.path_prefix):
                    best = service
        if best is not None:
            sub_path = path[len(best.path_prefix) :]
            return RouteMatch(service=best, sub_path=sub_path)

        for service in self.services:
            prefix = f"/{PASSTHROUGH_SEGMENT}/{service.name}"
            if path == prefix or path.startswith(prefix + "/"):
                sub_path = path[len(prefix) :]
                return RouteMatch(service=service, sub_path=sub_path, passthrough=True)

        return None

    def external_url_for(self, service: ServiceConfig) -> str:
        """Return the externally-visible alias URL for a service.

        Used to rewrite occurrences of the service's own alias path found
        in response bodies, so clients keep talking to this proxy for
        subsequent requests instead of bypassing it.
        """
        return f"{self.public_base_url}{self.base_path}{service.path_prefix}"

    def external_passthrough_url_for(self, service: ServiceConfig) -> str:
        """Return the externally-visible passthrough URL for a service.

        Used to rewrite occurrences of the service's upstream *origin*
        (regardless of path) found in response bodies -- e.g. ArcGIS's own
        canonical REST URLs embedded in Capabilities documents, which don't
        share the service's configured alias path at all.
        """
        return (
            f"{self.public_base_url}{self.base_path}"
            f"/{PASSTHROUGH_SEGMENT}/{service.name}"
        )


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
            default="https://backend-api.klienter-prod-k8s2.norgeibilder.no/token/tilecache",
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
        base_path=env.str("BASE_PATH", default=""),
        public_base_url=env.str("PUBLIC_BASE_URL", default=""),
    )
