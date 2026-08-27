"""Tests for the service registry matching logic in config.py."""

from nib_proxy.config import ServiceConfig, Settings


def _settings() -> Settings:
    return Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        cache_max_entries=10,
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
    assert match.passthrough is False


def test_match_service_with_sub_path():
    settings = _settings()
    match = settings.match_service("/wmts/utm32/1/2/3.png")
    assert match is not None
    assert match.service.name == "wmts-utm32"
    assert match.sub_path == "/1/2/3.png"


def test_match_service_returns_none_when_unmatched():
    settings = _settings()
    assert settings.match_service("/unknown/path") is None


def test_match_service_passthrough_prefix():
    settings = _settings()
    match = settings.match_service(
        "/_upstream/wmts-utm32/arcgis/rest/services/foo/MapServer/WMTS"
    )
    assert match is not None
    assert match.service.name == "wmts-utm32"
    assert match.sub_path == "/arcgis/rest/services/foo/MapServer/WMTS"
    assert match.passthrough is True


def test_alias_prefix_takes_priority_over_passthrough():
    settings = _settings()
    # A normal alias match should never be treated as passthrough, even if
    # some other service's name happens to appear in the path.
    match = settings.match_service("/wmts/utm32/foo")
    assert match is not None
    assert match.passthrough is False


def test_service_config_strips_trailing_slashes():
    service = ServiceConfig(
        name="x",
        path_prefix="/foo/",
        upstream="https://example.com/bar/",
    )
    assert service.path_prefix == "/foo"
    assert service.upstream == "https://example.com/bar"


def test_service_config_computes_origin():
    service = ServiceConfig(
        name="x",
        path_prefix="/foo",
        upstream="https://example.com:8443/bar/baz",
    )
    assert service.origin == "https://example.com:8443"


def test_external_passthrough_url_for():
    settings = Settings(
        nib_username="",
        nib_password="",
        token_url="https://example.com/token",
        token_validity_seconds=3600,
        cache_max_entries=10,
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
        settings.external_passthrough_url_for(service)
        == "https://proxy.example.org/nib/_upstream/wmts-utm32"
    )
