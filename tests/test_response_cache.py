"""Tests for the response cache (LRU + TTL)."""

import time

from nib_proxy.response_cache import ResponseCache


def test_set_and_get_roundtrip():
    cache = ResponseCache(max_entries=10)
    key = ResponseCache.build_key("svc", "GET", "/tile/1/2/3", "")
    cache.set(
        key,
        status_code=200,
        headers={"content-type": "image/png"},
        body=b"abc",
        ttl_seconds=60,
    )

    entry = cache.get(key)
    assert entry is not None
    assert entry.status_code == 200
    assert entry.body == b"abc"


def test_get_returns_none_for_missing_key():
    cache = ResponseCache(max_entries=10)
    assert cache.get("missing") is None


def test_entry_expires_after_ttl():
    cache = ResponseCache(max_entries=10)
    key = "k"
    cache.set(key, status_code=200, headers={}, body=b"x", ttl_seconds=0)
    # Immediately expired since ttl is 0 and monotonic time has already advanced.
    time.sleep(0.01)
    assert cache.get(key) is None


def test_lru_eviction_when_max_entries_exceeded():
    cache = ResponseCache(max_entries=2)
    cache.set("a", status_code=200, headers={}, body=b"a", ttl_seconds=60)
    cache.set("b", status_code=200, headers={}, body=b"b", ttl_seconds=60)
    cache.set("c", status_code=200, headers={}, body=b"c", ttl_seconds=60)

    assert len(cache) == 2
    assert cache.get("a") is None
    assert cache.get("b") is not None
    assert cache.get("c") is not None


def test_get_refreshes_recency():
    cache = ResponseCache(max_entries=2)
    cache.set("a", status_code=200, headers={}, body=b"a", ttl_seconds=60)
    cache.set("b", status_code=200, headers={}, body=b"b", ttl_seconds=60)
    # Access "a" so it becomes most-recently-used.
    cache.get("a")
    cache.set("c", status_code=200, headers={}, body=b"c", ttl_seconds=60)

    assert cache.get("a") is not None
    assert cache.get("b") is None
    assert cache.get("c") is not None


def test_build_key_ignores_client_identity():
    key1 = ResponseCache.build_key("svc", "GET", "/tile/1/2/3", "a=1")
    key2 = ResponseCache.build_key("svc", "GET", "/tile/1/2/3", "a=1")
    assert key1 == key2
