"""Content-addressed image cache with 320 px thumbnails and LRU eviction.

Files live under `data/images/`, keyed by the SHA-256 of the normalized URL, and
the `image_cache` table is the accounting source (size, dimensions, state, last
use).  Files referenced by notifications inside retention are never evicted.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from PIL import Image, UnidentifiedImageError

from ..core.models import ImageCacheEntry, ImageState, now_ms

logger = logging.getLogger("bark_pc.images.cache")

THUMBNAIL_WIDTH = 320
DEFAULT_CACHE_MB = 200
ORPHAN_MAX_AGE_DAYS = 7
DEFAULT_PORTS = {"http": 80, "https": 443}
EXTENSIONS = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp", "BMP": "bmp"}
MAX_DIMENSION = 10_000


def normalize_url(url: str) -> str:
    """Stable normalization used as the cache key."""
    text = (url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port and DEFAULT_PORTS.get(scheme) != port:
        host = f"{host}:{port}"
    path = parts.path or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


class ImageStore:
    """Files + database rows for cached images."""

    def __init__(
        self,
        db,
        images_dir: str | Path,
        *,
        cache_limit_mb: int = DEFAULT_CACHE_MB,
        thumbnail_width: int = THUMBNAIL_WIDTH,
    ):
        self.db = db
        self.images_dir = Path(images_dir)
        self.cache_limit_mb = int(cache_limit_mb)
        self.thumbnail_width = int(thumbnail_width)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    # -- lookups ----------------------------------------------------------- #
    def get(self, url: str) -> ImageCacheEntry | None:
        return self.db.get_image_by_hash(url_hash(url))

    def get_by_id(self, image_id: int | None) -> ImageCacheEntry | None:
        if image_id is None:
            return None
        return self.db.get_image(image_id)

    def file_path(self, entry: ImageCacheEntry) -> Path | None:
        if not entry.path:
            return None
        path = self.images_dir / entry.path
        return path if path.exists() else None

    def thumbnail_path(self, entry: ImageCacheEntry) -> Path | None:
        if not entry.thumb_path:
            return None
        path = self.images_dir / entry.thumb_path
        return path if path.exists() else None

    def is_cached(self, url: str) -> bool:
        entry = self.get(url)
        return bool(entry and entry.state == ImageState.CACHED and self.file_path(entry))

    def total_bytes(self) -> int:
        return self.db.image_cache_bytes()

    # -- writes ------------------------------------------------------------ #
    def store(self, url: str, data: bytes) -> ImageCacheEntry:
        """Decode *data* and persist it plus a thumbnail. Raises on bad images."""
        digest = url_hash(url)
        image = _decode(data)
        extension = EXTENSIONS.get((image.format or "PNG").upper(), "png")
        filename = f"{digest}.{extension}"
        path = self.images_dir / filename
        path.write_bytes(data)
        thumb_name = f"{digest}.thumb.png"
        thumb_path = self.images_dir / thumb_name
        thumbnail = image.copy()
        thumbnail.thumbnail((self.thumbnail_width, MAX_DIMENSION))
        if thumbnail.mode not in ("RGB", "RGBA"):
            thumbnail = thumbnail.convert("RGBA")
        thumbnail.save(thumb_path, format="PNG", optimize=True)

        total = path.stat().st_size + thumb_path.stat().st_size
        timestamp = now_ms()
        entry = ImageCacheEntry(
            url_hash=digest,
            url=url,
            path=filename,
            bytes=total,
            width=image.width,
            height=image.height,
            thumb_path=thumb_name,
            state=ImageState.CACHED,
            fetched_at=timestamp,
            last_used_at=timestamp,
        )
        entry = self.db.upsert_image(entry)
        self.db.set_image_for_url(url, entry.id) if entry.id is not None else None
        self.evict()
        return entry

    def mark_failed(self, url: str, state: ImageState, *, reason: str = "") -> ImageCacheEntry:
        digest = url_hash(url)
        existing = self.db.get_image_by_hash(digest)
        timestamp = now_ms()
        entry = ImageCacheEntry(
            id=existing.id if existing else None,
            url_hash=digest,
            url=url,
            state=state,
            fetched_at=timestamp,
            last_used_at=timestamp,
        )
        entry = self.db.upsert_image(entry)
        logger.info("image %s… %s%s", digest[:8], state.value, f": {reason}" if reason else "")
        return entry

    def touch(self, entry: ImageCacheEntry) -> None:
        if entry.id is None:
            return
        self.db.touch_image(entry.id)
        entry.last_used_at = now_ms()

    # -- maintenance ------------------------------------------------------- #
    def evict(self, *, protected_hashes: set[str] | None = None) -> int:
        """Evict least-recently-used cached images until under the size limit."""
        limit = self.cache_limit_mb * 1024 * 1024
        if self.total_bytes() <= limit:
            return 0
        protected = set(protected_hashes or set())
        protected |= {url_hash(url) for url in self.db.referenced_image_urls()}
        protected |= {
            entry.url_hash
            for entry in (
                self.db.get_image(image_id) for image_id in self.db.referenced_image_ids()
            )
            if entry is not None
        }
        removed = 0
        for entry in self.db.list_images_lru():
            if self.total_bytes() <= limit:
                break
            if entry.url_hash in protected:
                continue
            self._delete_files(entry)
            self.db.delete_image(entry.id)
            removed += 1
        if removed:
            logger.info(
                "evicted %s cached image(s) to stay under %s MB", removed, self.cache_limit_mb
            )
        return removed

    def purge_orphans(
        self, *, max_age_days: int = ORPHAN_MAX_AGE_DAYS, now: int | None = None
    ) -> int:
        current = now or now_ms()
        cutoff = current - max_age_days * 24 * 60 * 60 * 1000
        referenced = self.db.referenced_image_ids()
        removed = 0
        for entry in self.db.list_images_lru(state=None):
            if entry.id in referenced:
                continue
            last_used = entry.last_used_at or entry.fetched_at or 0
            if last_used >= cutoff:
                continue
            self._delete_files(entry)
            self.db.delete_image(entry.id)
            removed += 1
        if removed:
            logger.info("purged %s orphaned image file(s)", removed)
        return removed

    def _delete_files(self, entry: ImageCacheEntry) -> None:
        for name in (entry.path, entry.thumb_path):
            if not name:
                continue
            path = self.images_dir / name
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # pragma: no cover - defensive
                logger.debug("could not remove %s: %s", path, exc)


def _decode(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise InvalidImage(str(exc)) from exc


class InvalidImage(ValueError):
    pass
