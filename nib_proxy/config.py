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
class ServiceConfig:
    """A single proxied upstream service."""

    name: str
    path_prefix: str
    upstream: str

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
class RouteMatch:
    """Result of matching an inbound path against the service registry."""

    service: ServiceConfig
    sub_path: str


@dataclass(frozen=True)
class Settings:
    """Global proxy settings."""

    nib_username: str
    nib_password: str
    token_url: str
    token_validity_seconds: int
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

        Returns the matching service and remaining sub-path, or ``None`` if
        no service matches. The longest matching prefix wins.
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
        return RouteMatch(service=best, sub_path=sub_path)

    def external_url_for(self, service: ServiceConfig) -> str:
        """Return the externally-visible URL for a service through this proxy.

        Used to rewrite occurrences of the service's own upstream URL found
        in response bodies (e.g. WMS/WMTS Capabilities documents), so
        clients keep talking to this proxy for subsequent requests instead
        of bypassing it.
        """
        return f"{self.public_base_url}{self.base_path}{service.path_prefix}"


def _load_services(config_path: pathlib.Path) -> tuple[ServiceConfig, ...]:
    if not config_path.exists():
        return ()

    text = config_path.read_text()
    if config_path.suffix in (".yaml", ".yml"):
        raw = yaml.safe_load(text) or []
    else:
        raw = json.loads(text or "[]")

    return tuple(
        ServiceConfig(
            name=entry["name"],
            path_prefix=entry["path_prefix"],
            upstream=entry["upstream"],
        )
        for entry in raw
    )


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
