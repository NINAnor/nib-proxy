"""Tests for the service registry matching logic in config.py."""

from nib_proxy.config import ServiceConfig, Settings


def _settings() -> Settings:
    return Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        services=(
            ServiceConfig(
                name="wms-ortofoto",
                path_prefix="/wms/ortofoto",
                upstream="https://services.norgeibilder.no/wms/ortofoto",
            ),
            ServiceConfig(
                name="wmts-utm32",
                path_prefix="/wmts/utm32",
                upstream="https://tilecache.norgeibilder.no/wmts/utm32_euref89",
            ),
        ),
    )


def test_match_service_exact_prefix():
    settings = _settings()
    match = settings.match_service("/wms/ortofoto")
    assert match is not None
    assert match.service.name == "wms-ortofoto"
    assert match.sub_path == ""


def test_match_service_with_sub_path():
    settings = _settings()
    match = settings.match_service("/wmts/utm32/1/2/3.png")
    assert match is not None
    assert match.service.name == "wmts-utm32"
    assert match.sub_path == "/1/2/3.png"


def test_match_service_returns_none_when_unmatched():
    settings = _settings()
    assert settings.match_service("/unknown/path") is None


def test_service_config_strips_trailing_slashes():
    service = ServiceConfig(
        name="x",
        path_prefix="/foo/",
        upstream="https://example.com/bar/",
    )
    assert service.path_prefix == "/foo"
    assert service.upstream == "https://example.com/bar"


def test_external_url_for_combines_public_base_url_base_path_and_prefix():
    settings = Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        services=(),
        base_path="/nib",
        public_base_url="https://proxy.example.org",
    )
    service = ServiceConfig(
        name="wmts-utm32",
        path_prefix="/wmts/utm32",
        upstream="https://tilecache.norgeibilder.no/wmts/utm32_euref89",
    )
    assert (
        settings.external_url_for(service) == "https://proxy.example.org/nib/wmts/utm32"
    )


def test_allows_referrer_matches_host_and_path_prefix():
    settings = Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        services=(),
        allowed_referrers=("https://maps.example.org/viewer",),
    )

    assert settings.allows_referrer("https://maps.example.org/viewer/map/42")
    assert not settings.allows_referrer("https://maps.example.org/other")
    assert not settings.allows_referrer("https://other.example.org/viewer")


def test_allows_ip_matches_addresses_and_cidr_ranges():
    settings = Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        services=(),
        allowed_ips=("203.0.113.10", "2001:db8::/32"),
    )

    assert settings.allows_ip("203.0.113.10")
    assert settings.allows_ip("2001:db8:1::1")
    assert not settings.allows_ip("203.0.113.11")
