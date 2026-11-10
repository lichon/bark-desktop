"""Thread-owning TTS provider: one worker thread, one engine, a tiny mailbox.

COM speech engines belong to the apartment (thread) that created them, and a
slow voice must never stall the ingest or UI thread.  This provider therefore
creates the engine on its own thread and only ever pushes work onto a bounded
queue.  Requests older than the newest one are dropped: a burst of twenty pushes
should say the last thing, not replay the backlog.
"""

from __future__ import annotations

import logging
import queue
import threading

logger = logging.getLogger("bark_pc.tts")

MAX_PENDING = 4


class ThreadedTtsProvider:
    """Wraps an engine factory (`**{rate, volume} -> engine`) in a worker thread.

    Engine protocol: `speak(text)`, `stop()`, `set_rate(int)`, `set_volume(int)`,
    `close()`.
    """

    def __init__(self, engine_factory, *, name: str = "tts", rate: int = 0,
                 volume: int = 100, log: logging.Logger | None = None):
        self.name = name
        self._factory = engine_factory
        self._rate = int(rate)
        self._volume = int(volume)
        self._log = log or logger
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_PENDING)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._available: bool | None = None
        self.last_error: str | None = None

    # -- public API -------------------------------------------------------- #
    def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        if self._available is False:
            return False
        self._ensure_worker()
        self._put(("speak", text))
        return True

    def stop(self) -> None:
        if self._thread is not None:
            self._put(("stop", None))

    def configure(self, *, rate: int | None = None, volume: int | None = None) -> None:
        changed = False
        if rate is not None and int(rate) != self._rate:
            self._rate = int(rate)
            changed = True
        if volume is not None and int(volume) != self._volume:
            self._volume = int(volume)
            changed = True
        if changed and self._thread is not None and self._available is not False:
            self._put(("configure", None))

    def available(self, timeout: float = 3.0) -> bool:
        """Start the engine and report whether it came up (blocking probe)."""
        self._ensure_worker()
        self._ready.wait(timeout)
        return bool(self._available)

    def reset(self) -> None:
        """Forget a previous engine failure so the next `speak` retries."""
        with self._lock:
            if self._available is not False:
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._available = None
            self._thread = None
            self._ready.clear()
            self.last_error = None

    def shutdown(self, timeout: float = 2.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._put(("close", None))
        thread.join(timeout)
        if thread.is_alive():  # pragma: no cover - engine stuck in Speak
            self._log.debug("speech worker did not stop within %.1fs", timeout)
        self._thread = None

    # -- mailbox ----------------------------------------------------------- #
    def _put(self, item) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:  # pragma: no cover - racing consumer
                    pass

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._available is False:
                return
            self._thread = threading.Thread(
                target=self._run, name=f"bark-tts-{self.name}", daemon=True
            )
            self._thread.start()

    # -- worker ------------------------------------------------------------ #
    def _run(self) -> None:
        engine = None
        try:
            engine = self._factory(rate=self._rate, volume=self._volume)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._available = False
            self._ready.set()
            self._log.warning(
                "text-to-speech unavailable (%s provider): %s", self.name, self.last_error
            )
            return
        self._available = True
        self._ready.set()
        self._log.info("text-to-speech ready (%s provider)", self.name)
        try:
            while True:
                kind, payload = self._queue.get()
                try:
                    if kind == "close":
                        break
                    if kind == "speak":
                        engine.speak(payload)
                    elif kind == "stop":
                        engine.stop()
                    elif kind == "configure":
                        engine.set_rate(self._rate)
                        engine.set_volume(self._volume)
                except Exception as exc:  # a bad line must not kill the worker
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    self._log.warning("speech step failed (%s provider): %s", self.name, exc)
        finally:
            try:
                engine.close()
            except Exception:  # pragma: no cover - defensive
                self._log.debug("speech engine close failed", exc_info=True)
