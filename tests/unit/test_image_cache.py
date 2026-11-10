"""Cache hashing, thumbnails, accounting and orphan maintenance."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from bark_pc.core.models import ImageState, Notification, now_ms
from bark_pc.images.cache import (
    THUMBNAIL_WIDTH,
    ImageStore,
    InvalidImage,
    normalize_url,
    url_hash,
)


def image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "orange").save(buffer, format=fmt)
    return buffer.getvalue()


@pytest.fixture
def store(db, portable_paths) -> ImageStore:
    return ImageStore(db, portable_paths.images_dir, cache_limit_mb=200)


def test_url_normalization_is_stable():
    normalized = normalize_url("HTTPS://Example.COM:443/a/b?q=1#frag")
    assert normalized == "https://example.com/a/b?q=1"
    assert normalize_url("http://Example.com:8080/x") == "http://example.com:8080/x"
    assert normalize_url("https://example.com") == "https://example.com/"
    assert normalize_url("https://example.com/a?b=2&a=1") == normalize_url(
        "https://example.com/a?b=2&a=1"
    )


def test_hash_is_stable_across_equivalent_urls():
    assert url_hash("https://Example.com/a.png") == url_hash("https://example.com/a.png")
    assert url_hash("https://example.com/a.png#one") == url_hash("https://example.com/a.png#two")
    assert url_hash("https://example.com/a.png") != url_hash("https://example.com/b.png")
    assert len(url_hash("https://example.com/a.png")) == 64


def test_store_creates_a_320px_thumbnail_preserving_aspect(store):
    entry = store.store("https://example.com/wide.png", image_bytes(640, 320))

    assert entry.state == ImageState.CACHED
    assert entry.width == 640
    assert entry.height == 320
    thumb_path = store.thumbnail_path(entry)
    assert thumb_path is not None
    with Image.open(thumb_path) as thumbnail:
        assert thumbnail.width == THUMBNAIL_WIDTH
        assert thumbnail.height == THUMBNAIL_WIDTH // 2


def test_narrow_images_are_not_upscaled(store):
    entry = store.store("https://example.com/tiny.png", image_bytes(100, 50))
    with Image.open(store.thumbnail_path(entry)) as thumbnail:
        assert thumbnail.width == 100
        assert thumbnail.height == 50


def test_accounting_matches_file_sizes(store, db):
    entry = store.store("https://example.com/accounted.png", image_bytes(200, 100))
    on_disk = store.file_path(entry).stat().st_size + store.thumbnail_path(entry).stat().st_size
    assert entry.bytes == on_disk
    assert db.image_cache_bytes() == on_disk


def test_corrupt_data_raises_invalid_image(store):
    with pytest.raises(InvalidImage):
        store.store("https://example.com/broken.png", b"definitely not an image")


def test_failed_fetch_states_are_recorded(store):
    entry = store.mark_failed("https://example.com/missing.png", ImageState.UNAVAILABLE)
    assert entry.state == ImageState.UNAVAILABLE
    assert store.get("https://example.com/missing.png").state == ImageState.UNAVAILABLE


def test_store_attaches_the_image_to_matching_notifications(store, db):
    url = "https://example.com/shared.png"
    notification = Notification(
        remote_id="shared",
        title="t",
        body="b",
        image_url=url,
        received_at=now_ms(),
        source_server="api.day.app",
    )
    notification_id = db.insert_notification(notification)

    entry = store.store(url, image_bytes(120, 60))

    assert db.get_notification(notification_id).image_id == entry.id


def test_orphan_purge_removes_unreferenced_old_images(store, db):
    orphan = store.store("https://example.com/orphan.png", image_bytes(80, 40))
    kept = store.store("https://example.com/kept.png", image_bytes(80, 40))
    db.insert_notification(
        Notification(
            remote_id="kept",
            title="t",
            body="b",
            image_url="https://example.com/kept.png",
            image_id=kept.id,
            received_at=now_ms(),
            source_server="api.day.app",
        )
    )

    removed = store.purge_orphans(max_age_days=0, now=now_ms() + 1000)

    assert removed == 1
    assert store.get("https://example.com/orphan.png") is None
    assert store.get("https://example.com/kept.png") is not None
    assert store.file_path(orphan) is None
    assert store.file_path(kept) is not None


def test_touch_updates_last_used(store, db):
    entry = store.store("https://example.com/touch.png", image_bytes(50, 50))
    first_used = db.get_image(entry.id).last_used_at
    store.touch(entry)
    assert db.get_image(entry.id).last_used_at >= first_used


def test_missing_files_are_reported_as_missing(store):
    entry = store.store("https://example.com/gone.png", image_bytes(50, 50))
    store.file_path(entry).unlink()
    assert store.file_path(entry) is None
    assert store.is_cached("https://example.com/gone.png") is False
