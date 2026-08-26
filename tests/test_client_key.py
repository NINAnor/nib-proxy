"""Tests for client_key resolution."""

from starlette.requests import Request


def _make_request(
    headers: dict[str, str], client_host: str | None = "1.2.3.4"
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host else None,
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


def test_resolve_from_referer():
    from nib_proxy.client_key import resolve_client_key

    request = _make_request({"referer": "https://example.com/some/path"})
    key = resolve_client_key(request)
    assert key.mode == "referer"
    assert key.value == "example.com"


def test_resolve_from_origin_when_no_referer():
    from nib_proxy.client_key import resolve_client_key

    request = _make_request({"origin": "https://foo.bar"})
    key = resolve_client_key(request)
    assert key.mode == "referer"
    assert key.value == "foo.bar"


def test_resolve_from_ip_when_no_referer_or_origin():
    from nib_proxy.client_key import resolve_client_key

    request = _make_request({}, client_host="10.0.0.1")
    key = resolve_client_key(request)
    assert key.mode == "ip"
    assert key.value == "10.0.0.1"


def test_resolve_from_forwarded_for():
    from nib_proxy.client_key import resolve_client_key

    request = _make_request({"x-forwarded-for": "203.0.113.5, 10.0.0.1"})
    key = resolve_client_key(request)
    assert key.mode == "ip"
    assert key.value == "203.0.113.5"


def test_cache_key_distinguishes_mode_and_value():
    from nib_proxy.client_key import ClientKey

    a = ClientKey(mode="referer", value="example.com")
    b = ClientKey(mode="ip", value="example.com")
    assert a.cache_key != b.cache_key
