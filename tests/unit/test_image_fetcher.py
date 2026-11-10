"""Image fetching caps, cache hits, redirect limits and LRU eviction."""

from __future__ import annotations

import io

import httpx
import pytest
from PIL import Image

from bark_pc.core.models import ImageState, Notification, now_ms
from bark_pc.images.cache import ImageStore
from bark_pc.images.fetcher import MAX_BYTES, ImageFetcher


def png_bytes(width: int = 40, height: int = 20, color: str = "red") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def store(db, portable_paths) -> ImageStore:
    return ImageStore(db, portable_paths.images_dir, cache_limit_mb=200)


def fetcher_for(store, handler) -> tuple[ImageFetcher, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return ImageFetcher(store, transport=httpx.MockTransport(wrapped)), calls


def test_cached_url_causes_zero_transport_calls(store):
    url = "https://example.com/already.png"
    store.store(url, png_bytes())

    fetcher, calls = fetcher_for(store, lambda request: pytest.fail("transport must not be used"))
    entry = fetcher.fetch_now(url)

    assert entry.state == ImageState.CACHED
    assert calls == []


def test_body_over_ten_megabytes_is_too_large(store):
    big = b"x" * (MAX_BYTES + 1)
    fetcher, _calls = fetcher_for(
        store,
        lambda request: httpx.Response(200, content=big, headers={"content-type": "image/png"}),
    )
    entry = fetcher.fetch_now("https://example.com/huge.png")
    assert entry.state == ImageState.TOO_LARGE


def test_html_content_type_is_not_an_image(store):
    fetcher, _calls = fetcher_for(
        store,
        lambda request: httpx.Response(
            200, content=b"<html></html>", headers={"content-type": "text/html"}
        ),
    )
    entry = fetcher.fetch_now("https://example.com/page")
    assert entry.state == ImageState.NOT_IMAGE


def test_non_http_urls_are_not_fetched(store):
    fetcher, calls = fetcher_for(store, lambda request: pytest.fail("must not fetch"))
    entry = fetcher.fetch_now("file:///C:/secret.png")
    assert entry.state == ImageState.NOT_IMAGE
    assert calls == []


def test_redirect_cap_is_two(store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"{request.url}?again"})

    fetcher, calls = fetcher_for(store, handler)
    entry = fetcher.fetch_now("https://example.com/loop.png")

    assert entry.state == ImageState.UNAVAILABLE
    assert len(calls) == 3  # the original request plus two redirects


def test_successful_fetch_is_cached_and_reused(store):
    payload = png_bytes()
    fetcher, calls = fetcher_for(
        store,
        lambda request: httpx.Response(200, content=payload, headers={"content-type": "image/png"}),
    )
    first = fetcher.fetch_now("https://example.com/one.png")
    second = fetcher.fetch_now("https://example.com/one.png")

    assert first.state == second.state == ImageState.CACHED
    assert first.id == second.id
    assert len(calls) == 1
    assert store.is_cached("https://example.com/one.png")


def test_corrupt_image_body_is_unavailable(store):
    fetcher, _calls = fetcher_for(
        store,
        lambda request: httpx.Response(
            200, content=b"not really an image", headers={"content-type": "image/png"}
        ),
    )
    entry = fetcher.fetch_now("https://example.com/corrupt.png")
    assert entry.state == ImageState.UNAVAILABLE


def test_http_error_is_unavailable_not_an_exception(store):
    fetcher, _calls = fetcher_for(store, lambda request: httpx.Response(404, text="nope"))
    entry = fetcher.fetch_now("https://example.com/missing.png")
    assert entry.state == ImageState.UNAVAILABLE


def test_duplicate_requests_are_coalesced(store):
    fetcher, calls = fetcher_for(
        store,
        lambda request: httpx.Response(
            200, content=png_bytes(), headers={"content-type": "image/png"}
        ),
    )
    fetcher.start()
    try:
        fetcher.submit("https://example.com/coalesce.png")
        fetcher.submit("https://example.com/coalesce.png")
        assert fetcher.wait_idle(timeout=5)
    finally:
        fetcher.shutdown()
    assert len(calls) == 1


def test_lru_eviction_keeps_files_referenced_by_notifications(store, db):
    first = store.store("https://example.com/referenced.png", png_bytes(color="blue"))
    second = store.store("https://example.com/unreferenced.png", png_bytes(color="green"))

    notification = Notification(
        remote_id="with-image",
        title="t",
        body="b",
        image_url="https://example.com/referenced.png",
        image_id=first.id,
        received_at=now_ms(),
        source_server="api.day.app",
    )
    db.insert_notification(notification)

    store.cache_limit_mb = 0
    removed = store.evict()

    remaining = {entry.url_hash for entry in store.db.list_images_lru()}
    assert first.url_hash in remaining
    assert second.url_hash not in remaining
    assert removed == 1
    assert store.file_path(first) is not None
    assert store.file_path(second) is None


def test_queued_fetch_reports_through_the_callback(store):
    entries = []
    fetcher, _calls = fetcher_for(
        store,
        lambda request: httpx.Response(
            200, content=png_bytes(), headers={"content-type": "image/png"}
        ),
    )
    fetcher.start()
    try:
        fetcher.submit("https://example.com/callback.png", callback=entries.append)
        assert fetcher.wait_idle(timeout=5)
    finally:
        fetcher.shutdown()
    assert len(entries) == 1
    assert entries[0].state == ImageState.CACHED
