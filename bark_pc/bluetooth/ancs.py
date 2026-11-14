"""iPhone notifications over ANCS, using a USB Bluetooth adapter through Bumble.

The iPhone is Apple's Notification Provider (a GATT server), so this PC has to
be the Notification Consumer (a GATT client).  This PC is the **peripheral**: it
advertises an ANCS service solicitation named “Bark Desktop” and lets the iPhone
connect from its Settings → Bluetooth screen.

`BumbleAncsBackend` (in `ancs_backend.py`) does the radio work.  This module owns
the thread and the state machine, mirroring `sink.py`: Bumble needs its own
asyncio loop, the Tk thread only reads immutable snapshots, and every
notification is handed to `bark_pc.app` which funnels it through the normal
ingest path (history, toasts, TTS, webhooks).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

ANCS_ADVERTISED_NAME = "Bark Desktop"
# 16-bit Appearance value advertised with the accessory.  This is only the icon
# and label iOS shows in Settings → Bluetooth: it is not part of the ANCS
# handshake, and pairing works with or without it.  The value combines the
# Assigned Numbers category id with its subcategory (category << 6 | sub),
# so 0x00C0 is Watch/Generic Watch and 0x00C2 is Watch/Smartwatch.
ANCS_APPEARANCE = 0x00C0
RETRY_DELAY_S = 5.0
# Tearing the session down means disconnecting the iPhone, stopping advertising
# and releasing the USB adapter; each step in the backend is bounded, so this is
# a ceiling for the whole sequence rather than a guess at a single command.
STOP_TIMEOUT_S = 10.0
STOP_BUSY_MESSAGE = (
    "The USB Bluetooth adapter did not finish shutting down. Wait a moment and "
    "press Restart again."
)

logger = logging.getLogger("bark_pc.ancs")


class AncsError(RuntimeError):
    """A user-facing ANCS/Bumble problem (adapter, driver, pairing, service)."""


class AncsStatus(str, Enum):
    OFF = "off"
    STARTING = "starting"
    ADVERTISING = "advertising"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


ANCS_STATUS_LABELS = {
    AncsStatus.OFF: "Off",
    AncsStatus.STARTING: "Starting",
    AncsStatus.ADVERTISING: "Waiting for the iPhone",
    AncsStatus.CONNECTING: "Connecting",
    AncsStatus.CONNECTED: "Receiving notifications",
    AncsStatus.ERROR: "Error",
}

_ACTIVE_STATUSES = (
    AncsStatus.STARTING,
    AncsStatus.ADVERTISING,
    AncsStatus.CONNECTING,
    AncsStatus.CONNECTED,
    AncsStatus.ERROR,
)


@dataclass(frozen=True)
class AncsState:
    """Snapshot of the ANCS receiver, safe to read from any thread."""

    status: AncsStatus = AncsStatus.OFF
    adapter_name: str = ""
    device_name: str = ""
    device_address: str = ""
    detail: str = ""
    error: str = ""
    since: float = 0.0

    @property
    def is_running(self) -> bool:
        return self.status in _ACTIVE_STATUSES

    @property
    def is_connected(self) -> bool:
        return self.status is AncsStatus.CONNECTED


@dataclass(frozen=True)
class AncsPeerInfo:
    """An iPhone (or a nearby Apple device) seen while scanning."""

    address: str = ""
    name: str = ""
    rssi: int = 0
    apple: bool = False
    has_ancs: bool = False
    connectable: bool = True

    @property
    def label(self) -> str:
        return self.name or self.address or "the iPhone"


# Names that belong to Apple products that are not the notification source.
_NON_PHONE_APPLE_NAMES = (
    "airpods",
    "beats",
    "apple watch",
    "watch",
    "macbook",
    "imac",
    "mac mini",
    "mac studio",
    "mac pro",
    "homepod",
    "apple tv",
    "apple pencil",
    "airtag",
    "keyboard",
    "mouse",
    "trackpad",
)


def iphone_score(peer: AncsPeerInfo) -> int:
    """How likely *peer* is the iPhone we should connect to; -1 means never."""
    name = (peer.name or "").casefold()
    if "iphone" in name or "ipad" in name:
        return 100
    if name and any(hint in name for hint in _NON_PHONE_APPLE_NAMES):
        return -1
    if peer.has_ancs:
        return 90
    if peer.apple:
        return 50
    return -1


def advertisement_peer(
    *,
    address: str,
    name: str = "",
    rssi: int = 0,
    connectable: bool = True,
    apple: bool = False,
    has_ancs: bool = False,
    include_all: bool = False,
) -> AncsPeerInfo | None:
    """Turn one decoded advertisement into a candidate, or None to skip it.

    `include_all` disables the iPhone-only filter (used by the diagnostic CLI
    to show every device in range); `AncsWorker` keeps the default.
    Kept free of Bumble classes so the filtering rules are unit-testable.
    """
    if not address:
        return None
    peer = AncsPeerInfo(
        address=address,
        name=(name or "").strip(),
        rssi=int(rssi),
        apple=apple,
        has_ancs=has_ancs,
        connectable=connectable,
    )
    if include_all:
        return peer
    if not connectable or iphone_score(peer) < 0:
        return None
    return peer


@dataclass(frozen=True)
class AncsNotification:
    """One ANCS notification with the attributes the user needs."""

    uid: int
    app_id: str = ""
    app_name: str = ""
    title: str = ""
    subtitle: str = ""
    body: str = ""
    category: str = ""
    date_ms: int | None = None

    @property
    def display_title(self) -> str:
        app = (self.app_name or "").strip()
        title = (self.title or "").strip()
        if app and title:
            return f"{app} - {title}"
        return title or app or self.category or "iPhone notification"

    @property
    def remote_id(self) -> str:
        """Stable across duplicate iPhone re-notifications of the same alert."""
        suffix = str(self.date_ms) if self.date_ms else "0"
        return f"ancs-{self.uid}-{suffix}"

    def to_bark_payload(self) -> bytes:
        """Map ANCS attributes onto the Bark payload `Ingestor` already parses.

        The app name is folded into the title (`App - title`), so the payload
        leaves the subtitle empty instead of repeating the app name there.
        """
        title = self.display_title
        subtitle = ""
        body = (self.body or "").strip()
        if body == title:
            body = ""
        payload = {
            "aps": {
                "alert": {
                    "title": title,
                    "subtitle": subtitle,
                    "body": body,
                }
            },
            "id": self.remote_id,
        }
        if self.app_name:
            payload["group"] = self.app_name
        payload["ancs_category"] = self.category
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def describe_ancs_state(state: AncsState) -> str:
    """One line for the settings dialog."""
    if state.status is AncsStatus.OFF:
        return "Off — iPhone notifications are not received over Bluetooth."
    if state.status is AncsStatus.STARTING:
        return state.detail or "Opening the USB Bluetooth adapter…"
    if state.status is AncsStatus.ADVERTISING:
        return state.detail or "Waiting for the iPhone (Settings → Bluetooth)."
    if state.status is AncsStatus.CONNECTING:
        name = state.device_name or state.device_address or "the iPhone"
        return state.detail or f"{name} connected; setting up notifications…"
    if state.status is AncsStatus.CONNECTED:
        name = state.device_name or state.device_address or "the iPhone"
        return f"Receiving notifications from {name}."
    return f"iPhone notifications error: {state.error or state.detail or 'unknown error'}"


class AncsBackend(Protocol):
    """The radio operations `AncsWorker` needs (faked in the unit tests)."""

    async def start(self) -> None: ...

    async def wait_for_connection(self, stop_event: asyncio.Event) -> AncsPeerInfo | None: ...

    async def subscribe(
        self,
        on_notification: Callable[[AncsNotification], None],
        on_disconnected: Callable[[], None],
    ) -> object: ...

    async def disconnect(self, handle: object | None = None) -> None: ...

    async def close(self) -> None: ...


class AncsWorker:
    """Owns the Bumble device and its asyncio loop.

    `backend_factory` exists so the tests can exercise the state machine without
    Bluetooth hardware; the production factory is `BumbleAncsBackend`.
    """

    def __init__(
        self,
        settings,
        *,
        log: logging.Logger | None = None,
        keystore_path=None,
        backend_factory: Callable[..., AncsBackend] | None = None,
        on_notification: Callable[[AncsNotification], None] | None = None,
        on_state: Callable[[AncsState], None] | None = None,
    ):
        self._settings = settings
        self._log = log or logger
        self._keystore_path = keystore_path
        self._backend_factory = backend_factory or _default_backend_factory
        self._on_notification = on_notification
        self._on_state = on_state

        self._state = AncsState()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Bumped by `start` and `stop`.  A thread may only touch the shared
        # loop/backend/state when it still owns the current generation: a wedged
        # teardown can outlive `stop`'s timeout and must not clobber the session
        # that replaces it.
        self._generation = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._backend: AncsBackend | None = None

    # -- public API -------------------------------------------------------- #
    @property
    def settings(self):
        return self._settings

    def state(self) -> AncsState:
        with self._state_lock:
            return self._state

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the ANCS receiver (no-op when already running)."""
        if self.is_running:
            return
        self._stop_requested.clear()
        self._loop_ready.clear()
        self._generation += 1
        self._set_state(AncsStatus.STARTING, detail="Opening the USB Bluetooth adapter…")
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(self._generation,),
            name="bark-ancs",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        """Stop the receiver; False when the thread outlived *timeout*.

        The receiver owns a USB adapter, so a restart may only reopen it once
        the old thread has released it.  A wedged controller can leave the
        teardown waiting on an HCI event that never arrives, hence the flag: a
        caller that sees False must not start a second session on that adapter.
        """
        self._generation += 1  # the running thread may no longer touch our state
        self._stop_requested.set()
        self._loop_ready.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass
        thread, self._thread = self._thread, None
        stopped = True
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                stopped = False
                # Keep it registered so `is_running` stays True and a later
                # `start` cannot open the same adapter behind its back.
                self._thread = thread
                self._log.warning(
                    "the ANCS receiver thread did not stop within %.1fs; "
                    "the adapter is still held",
                    timeout,
                )
        self._loop = None
        self._set_state(AncsStatus.OFF, detail="")
        return stopped

    def restart(self, timeout: float = STOP_TIMEOUT_S) -> None:
        """Restart the receiver, never starting a second session on a busy adapter."""
        if not self.stop(timeout):
            self._set_state(
                AncsStatus.ERROR,
                adapter_name=self._settings.ancs_adapter_name,
                error=STOP_BUSY_MESSAGE,
                detail=STOP_BUSY_MESSAGE,
            )
            self._log.warning("ancs: %s", STOP_BUSY_MESSAGE)
            return
        if self._settings.ancs_enabled:
            self.start()

    def configure(self, settings) -> None:
        """Apply saved settings; restart only when the Bluetooth setup changed."""
        previous, self._settings = self._settings, settings
        if not settings.ancs_enabled:
            self.stop()
            return
        if not previous.ancs_enabled:
            self.restart()
            return
        if not self.is_running:
            self.start()
            return
        if previous.ancs_adapter != settings.ancs_adapter:
            self.restart()

    def reconnect_now(self) -> None:
        """Restart the receiver now (used by the settings dialog button)."""
        self.restart()

    # -- thread plumbing --------------------------------------------------- #
    def _thread_main(self, generation: int) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            pass
        except Exception:  # pragma: no cover - unexpected Bumble/asyncio failure
            self._log.exception("the ANCS receiver crashed")
            if generation == self._generation:
                self._set_state(
                    AncsStatus.ERROR,
                    error="The iPhone notification receiver stopped unexpectedly.",
                )
        finally:
            self._loop_ready.set()
            if self._thread is threading.current_thread():
                self._thread = None
            if generation == self._generation:
                self._loop = None
                self._backend = None
                if self._stop_requested.is_set():
                    self._set_state(AncsStatus.OFF, detail="")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._loop_ready.set()
        if self._stop_requested.is_set():
            return

        while not self._stop_requested.is_set():
            backend = self._backend_factory(
                self._log, self._keystore_path, self._settings.ancs_adapter
            )
            self._backend = backend
            try:
                await self._await_or_stop(backend.start(), 30.0)
                if self._stop_requested.is_set():
                    return
                self._set_state(
                    AncsStatus.ADVERTISING,
                    adapter_name=self._settings.ancs_adapter_name,
                    detail=_advertising_detail(self._settings.ancs_adapter_name),
                )
                stopped = await self._peripheral_loop(backend)
                if stopped:
                    return
            except AncsError as exc:
                self._set_state(
                    AncsStatus.ERROR,
                    adapter_name=self._settings.ancs_adapter_name,
                    error=str(exc),
                    detail=str(exc),
                )
                self._log.warning("ancs: %s", exc)
            except _Stopped:
                return
            except asyncio.TimeoutError:
                message = (
                    "The USB Bluetooth adapter did not answer. Unplug and replug it, "
                    "then press Restart."
                )
                self._set_state(
                    AncsStatus.ERROR,
                    adapter_name=self._settings.ancs_adapter_name,
                    error=message,
                    detail=message,
                )
                self._log.warning("ancs: %s", message)
            except Exception as exc:  # pragma: no cover - driver specific
                self._log.exception("ancs: the receiver loop failed")
                self._set_state(
                    AncsStatus.ERROR,
                    adapter_name=self._settings.ancs_adapter_name,
                    error=str(exc) or exc.__class__.__name__,
                    detail=str(exc),
                )
            finally:
                try:
                    await backend.close()
                except Exception:  # pragma: no cover - teardown
                    self._log.debug("closing the ANCS backend failed", exc_info=True)
                self._backend = None

            if self._stop_requested.is_set():
                return
            if not await self._wait_for_stop(RETRY_DELAY_S):
                return

    async def _peripheral_loop(self, backend: AncsBackend) -> bool:
        """Serve connections initiated by the iPhone until stop."""
        while not self._stop_requested.is_set():
            peer = await self._await_or_stop(
                backend.wait_for_connection(self._stop_event), None
            )
            if peer is None:
                return True
            self._set_state(
                AncsStatus.CONNECTING,
                adapter_name=self._settings.ancs_adapter_name,
                device_name=peer.name,
                device_address=peer.address,
                detail=f"{peer.label} connected; setting up notifications…",
            )
            stopped, ok = await self._serve_connection(backend, peer)
            if stopped:
                return True
            if not ok:
                if not await self._wait_for_stop(RETRY_DELAY_S):
                    return True
                continue
            self._set_state(
                AncsStatus.ADVERTISING,
                adapter_name=self._settings.ancs_adapter_name,
                detail=(
                    "The iPhone disconnected. Waiting for it to reconnect — keep "
                    "Bluetooth on and tap the accessory name in Settings → Bluetooth."
                ),
            )
        return True

    async def _serve_connection(
        self, backend: AncsBackend, peer: AncsPeerInfo
    ) -> tuple[bool, bool]:
        """Subscribe and wait for the drop.  Returns `(stopped, ready)`."""
        disconnected = asyncio.Event()
        try:
            await self._await_or_stop(
                backend.subscribe(self._emit_notification, disconnected.set),
                180.0,
            )
        except AncsError as exc:
            self._set_state(
                AncsStatus.ERROR,
                adapter_name=self._settings.ancs_adapter_name,
                device_name=peer.name,
                device_address=peer.address,
                error=str(exc),
                detail=str(exc),
            )
            self._log.warning("ancs: %s", exc)
            try:
                await backend.disconnect()
            except Exception:  # pragma: no cover - already gone
                self._log.debug("could not drop the failed connection", exc_info=True)
            return False, False
        except asyncio.TimeoutError:
            message = "Timed out while setting up the Apple notification service."
            self._set_state(
                AncsStatus.ERROR,
                adapter_name=self._settings.ancs_adapter_name,
                device_name=peer.name,
                device_address=peer.address,
                error=message,
                detail=message,
            )
            try:
                await backend.disconnect()
            except Exception:  # pragma: no cover - already gone
                pass
            return False, False

        self._set_state(
            AncsStatus.CONNECTED,
            adapter_name=self._settings.ancs_adapter_name,
            device_name=peer.name,
            device_address=peer.address,
            detail=f"Receiving notifications from {peer.label}.",
        )
        await self._wait_for_event(disconnected)
        if self._stop_requested.is_set():
            return True, True
        return False, True

    def _request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()

    async def _await_or_stop(self, awaitable, timeout: float | None):
        """Await *awaitable*, but give up when stop is requested or *timeout* hits."""
        assert self._stop_event is not None
        task = asyncio.ensure_future(awaitable)
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, stop_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            if stop_task in done:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise _Stopped()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise asyncio.TimeoutError()
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    async def _wait_for_event(self, event: asyncio.Event) -> None:
        assert self._stop_event is not None
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        event_task = asyncio.ensure_future(event.wait())
        try:
            await asyncio.wait({event_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_task.cancel()
            event_task.cancel()
            await asyncio.gather(stop_task, event_task, return_exceptions=True)

    async def _wait_for_stop(self, delay: float) -> bool:
        """Sleep *delay* seconds.  Returns False when stop was requested."""
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), delay)
            return False
        except asyncio.TimeoutError:
            return True

    # -- notifications ----------------------------------------------------- #
    def _emit_notification(self, notification: AncsNotification) -> None:
        callback = self._on_notification
        if callback is None:
            return
        try:
            callback(notification)
        except Exception:  # pragma: no cover - callback isolation
            self._log.exception("the ANCS notification callback failed")

    # -- state ------------------------------------------------------------- #
    def _set_state(
        self,
        status: AncsStatus,
        *,
        adapter_name: str | None = None,
        device_name: str | None = None,
        device_address: str | None = None,
        detail: str = "",
        error: str = "",
    ) -> None:
        with self._state_lock:
            previous = self._state
            self._state = AncsState(
                status=status,
                adapter_name=(previous.adapter_name if adapter_name is None else adapter_name),
                device_name=previous.device_name if device_name is None else device_name,
                device_address=(
                    previous.device_address if device_address is None else device_address
                ),
                detail=detail,
                error=error,
                since=time.monotonic(),
            )
            state = self._state
        if status is not previous.status:
            self._log.info("ancs: %s%s", status.value, f" ({detail})" if detail else "")
        callback = self._on_state
        if callback is not None:
            try:
                callback(state)
            except Exception:  # pragma: no cover - callback isolation
                self._log.exception("the ANCS state callback failed")


class _Stopped(Exception):
    """Internal: stop was requested while waiting for a radio operation."""


def _advertising_detail(adapter_name: str) -> str:
    where = f" on {adapter_name}" if adapter_name else ""
    return (
        f"Advertising{where}. On the iPhone, open Settings → Bluetooth, tap "
        f"“{ANCS_ADVERTISED_NAME}” under Other Devices or Accessories, and allow "
        "pairing and notifications."
    )


def _default_backend_factory(log: logging.Logger, keystore_path, adapter: str):
    from .ancs_backend import BumbleAncsBackend  # noqa: PLC0415 - optional dependency

    return BumbleAncsBackend(log=log, keystore_path=keystore_path, adapter=adapter)


def notification_from_attributes(
    *,
    uid: int,
    category: str,
    attributes: list[tuple[object, object]],
    app_name: str = "",
) -> AncsNotification:
    """Build an `AncsNotification` from `(attribute_id, value)` pairs.

    Kept independent of Bumble's classes so the mapping is unit-testable and so
    a missing attribute (iOS truncates or omits fields) never raises.
    """
    values: dict[str, object] = {}
    for attribute_id, value in attributes:
        name = getattr(attribute_id, "name", str(attribute_id))
        values[name] = value

    def text(name: str) -> str:
        value = values.get(name, "")
        if value is None:
            return ""
        return str(value).strip()

    date_ms = None
    raw_date = values.get("DATE")
    if isinstance(raw_date, datetime):
        try:
            date_ms = int(raw_date.timestamp() * 1000)
        except (OverflowError, OSError, ValueError):  # pragma: no cover - odd clocks
            date_ms = None

    return AncsNotification(
        uid=uid,
        app_id=text("APP_IDENTIFIER"),
        app_name=app_name,
        title=text("TITLE"),
        subtitle=text("SUBTITLE"),
        body=text("MESSAGE"),
        category=category,
        date_ms=date_ms,
    )
