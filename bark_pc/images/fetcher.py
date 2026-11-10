"""Bounded image fetching on two worker threads.

Never blocks the receiver: the supervisor simply hands URLs to a queue.  Fetches
are capped by content type, size and redirects so a hostile or broken URL cannot
freeze the app or fill the disk.  Failures become cache states, not exceptions.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ..core.http_debug import client_event_hooks
from ..core.models import ImageCacheEntry, ImageState
from .cache import ImageStore, InvalidImage, normalize_url

logger = logging.getLogger("bark_pc.images.fetcher")

MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 2
CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 20.0
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 bark-desktop/0.1"
)
TERMINAL_STATES = (ImageState.CACHED, ImageState.TOO_LARGE, ImageState.NOT_IMAGE)


@dataclass(order=True)
class _Job:
    priority: int
    sequence: int
    url: str = field(compare=False)
    callback: Callable[[ImageCacheEntry], None] | None = field(compare=False, default=None)


class ImageFetcher:
    """Thread pool around `ImageStore`; also exposes a synchronous `fetch_now`."""

    def __init__(
        self,
        store: ImageStore,
        *,
        transport: httpx.BaseTransport | None = None,
        thread_count: int = 2,
        client: httpx.Client | None = None,
    ):
        self.store = store
        self._transport = transport
        self._client = client or httpx.Client(
            transport=transport,
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
            headers={"User-Agent": BROWSER_UA, "Accept": "image/*,*/*;q=0.8"},
            event_hooks=client_event_hooks(),
        )
        self._queue: queue.PriorityQueue[_Job] = queue.PriorityQueue()
        self._threads: list[threading.Thread] = []
        self._thread_count = thread_count
        self._sequence = 0
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._stopping = threading.Event()
        self._busy = threading.Event()

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self._threads:
            return
        self._stopping.clear()
        for index in range(self._thread_count):
            thread = threading.Thread(target=self._worker, name=f"bark-image-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def shutdown(self, timeout: float = 3.0) -> None:
        self._stopping.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait(_Job(10_000, 0, "", None))
            except queue.Full:  # pragma: no cover - unbounded queue
                pass
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            pass

    def __enter__(self) -> ImageFetcher:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.shutdown()

    # -- queueing ---------------------------------------------------------- #
    def submit(
        self,
        url: str,
        *,
        priority: int = 0,
        callback: Callable[[ImageCacheEntry], None] | None = None,
    ) -> ImageCacheEntry | None:
        """Queue a fetch; returns a terminal cache entry when already resolved."""
        cached = self.store.get(url)
        if cached is not None and cached.state in TERMINAL_STATES:
            self.store.touch(cached)
            return cached
        digest = normalize_url(url)
        with self._lock:
            if digest in self._inflight:
                return cached
            self._inflight.add(digest)
            self._sequence += 1
            sequence = self._sequence
        self._queue.put(_Job(-int(priority), sequence, url, callback))
        return None

    def _worker(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._busy.clear()
                continue
            if not job.url or self._stopping.is_set():
                with self._lock:
                    self._inflight.discard(normalize_url(job.url))
                continue
            self._busy.set()
            try:
                entry = self.fetch_now(job.url)
            except Exception:  # pragma: no cover - fetch_now is already total
                logger.exception("image worker crashed")
                entry = self.store.mark_failed(job.url, ImageState.UNAVAILABLE)
            finally:
                with self._lock:
                    self._inflight.discard(normalize_url(job.url))
            if job.callback is not None:
                try:
                    job.callback(entry)
                except Exception:  # pragma: no cover - callback isolation
                    logger.exception("image callback failed")
            self._busy.clear()

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Block until the queue drains (used by tests and shutdown)."""
        import time

        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._queue.empty() and not self._busy.is_set():
                return True
            time.sleep(0.02)
        return False

    # -- synchronous fetch -------------------------------------------------- #
    def fetch_now(self, url: str) -> ImageCacheEntry:
        """Fetch and cache one URL; always returns an entry, never raises."""
        if not url:
            return self.store.mark_failed("", ImageState.UNAVAILABLE, reason="empty URL")
        existing = self.store.get(url)
        if existing is not None and existing.state in TERMINAL_STATES:
            self.store.touch(existing)
            return existing

        try:
            data = self._download(url)
        except _TooLarge:
            return self.store.mark_failed(url, ImageState.TOO_LARGE, reason="over 10 MB")
        except _NotImage as exc:
            return self.store.mark_failed(url, ImageState.NOT_IMAGE, reason=str(exc))
        except _Unavailable as exc:
            return self.store.mark_failed(url, ImageState.UNAVAILABLE, reason=str(exc))
        except Exception as exc:
            return self.store.mark_failed(url, ImageState.UNAVAILABLE, reason=str(exc))

        try:
            return self.store.store(url, data)
        except InvalidImage as exc:
            return self.store.mark_failed(url, ImageState.UNAVAILABLE, reason=str(exc))
        except OSError as exc:
            return self.store.mark_failed(url, ImageState.UNAVAILABLE, reason=str(exc))

    def _download(self, url: str) -> bytes:
        if not url.lower().startswith(("http://", "https://")):
            raise _NotImage("only http(s) URLs are fetched")
        with self._client.stream("GET", url) as response:
            if response.status_code != 200:
                raise _Unavailable(f"HTTP {response.status_code}")
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
            if not content_type.lower().startswith("image/"):
                raise _NotImage(f"content type is {content_type or 'unknown'}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                raise _TooLarge()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise _TooLarge()
                chunks.append(chunk)
        return b"".join(chunks)


class _TooLarge(Exception):
    pass


class _NotImage(Exception):
    pass


class _Unavailable(Exception):
    pass
