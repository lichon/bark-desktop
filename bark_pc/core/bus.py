"""Thread-safe plumbing between the receiver, workers and the Tk UI.

Three event loops exist in this application (Tk on the main thread, pystray's
Win32 loop, and an anyio loop on the receiver thread).  Nothing crosses a loop
boundary except through this module: plain queues for data and a single
`post_to_ui()` helper for widget work.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import ConnectionStatus, Notification, PushStatus, now_ms


@dataclass
class StatusEvent:
    status: PushStatus
    reason: str | None = None
    at: int = field(default_factory=now_ms)

    @property
    def connection_status(self) -> ConnectionStatus:
        return {
            PushStatus.CONNECTING: ConnectionStatus.CONNECTING,
            PushStatus.ACTIVE: ConnectionStatus.ACTIVE,
            PushStatus.DEGRADED: ConnectionStatus.DEGRADED,
            PushStatus.INVALID: ConnectionStatus.INVALID,
            PushStatus.STOPPED: ConnectionStatus.STARTING,
        }[self.status]


@dataclass
class NotificationEvent:
    notification: Notification
    at: int = field(default_factory=now_ms)


@dataclass
class TokenEvent:
    """The courier handed us a (possibly refreshed) base push token."""

    base_token_hex: str
    at: int = field(default_factory=now_ms)


@dataclass
class DeleteEvent:
    remote_id: str
    at: int = field(default_factory=now_ms)


@dataclass
class ImageReadyEvent:
    image_url: str
    image_id: int | None
    state: str
    at: int = field(default_factory=now_ms)


class BoundedEventQueue:
    """Bounded FIFO that drops the *oldest* item instead of blocking.

    The receiver must never block on a slow UI, and acks must never be delayed by
    a full queue (FR-008), so overflow is counted and the oldest event is
    discarded.
    """

    def __init__(self, maxsize: int = 512):
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self._lock = threading.Lock()
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def put(self, item: Any) -> bool:
        """Enqueue *item*; returns False when an older item had to be dropped."""
        if self._closed:
            return False
        dropped = False
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
                dropped = True
            except queue.Empty:  # pragma: no cover - race with a consumer
                pass
            with self._lock:
                self._dropped += 1
            try:
                self._queue.put_nowait(item)
            except queue.Full:  # pragma: no cover - defensive
                return False
        return not dropped

    def get(self, timeout: float | None = None) -> Any:
        return self._queue.get(timeout=timeout)

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break


# --------------------------------------------------------------------------- #
# UI marshalling
# --------------------------------------------------------------------------- #

_ui_scheduler: Callable[[Callable[[], None]], None] | None = None
_ui_scheduler_lock = threading.Lock()


def configure_ui_scheduler(scheduler: Callable[[Callable[[], None]], None] | None) -> None:
    """Install the function that marshals a callable onto the Tk thread."""
    global _ui_scheduler
    with _ui_scheduler_lock:
        _ui_scheduler = scheduler


def post_to_ui(func: Callable[[], None]) -> None:
    """Run *func* on the UI thread (or inline when no UI is running)."""
    with _ui_scheduler_lock:
        scheduler = _ui_scheduler
    if scheduler is None:
        try:
            func()
        except Exception:  # pragma: no cover - UI-less contexts
            pass
        return
    try:
        scheduler(func)
    except Exception:  # pragma: no cover - UI torn down mid-push
        pass


class UiMarshaller:
    """`root.after`-based scheduler that survives a destroyed Tk root."""

    def __init__(self, root):
        self._root = root
        self._closed = False

    def __call__(self, func: Callable[[], None]) -> None:
        if self._closed:
            return
        try:
            self._root.after(0, self._run, func)
        except Exception:  # pragma: no cover - Tk already destroyed
            self._closed = True

    def _run(self, func: Callable[[], None]) -> None:
        try:
            func()
        except Exception:  # pragma: no cover - a UI error must never kill the receiver
            import logging

            logging.getLogger("bark_pc.ui").exception("UI callback failed")

    def close(self) -> None:
        self._closed = True
