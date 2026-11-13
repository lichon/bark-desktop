"""Thread-owning controller for the Windows Bluetooth audio sink.

WinRT calls must not run on the Tk thread and `open_async()` can wait for the
phone for several seconds, so the connection lives on its own asyncio loop in a
daemon thread.  The rest of the application only reads an immutable `SinkState`
snapshot (the settings dialog polls it) and posts connect/disconnect commands
back to the loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from .winrt_backend import (
    BluetoothDeviceInfo,
    BluetoothError,
    WinRtPlaybackBackend,
)

logger = logging.getLogger("bark_pc.bluetooth")


class SinkStatus(str, Enum):
    OFF = "off"
    STARTING = "starting"
    READY = "ready"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


SINK_STATUS_LABELS = {
    SinkStatus.OFF: "Off",
    SinkStatus.STARTING: "Starting",
    SinkStatus.READY: "Waiting for a device",
    SinkStatus.CONNECTING: "Connecting",
    SinkStatus.CONNECTED: "Receiving audio",
    SinkStatus.ERROR: "Error",
}

# Statuses in which the worker thread is alive.
_ACTIVE_STATUSES = (
    SinkStatus.STARTING,
    SinkStatus.READY,
    SinkStatus.CONNECTING,
    SinkStatus.CONNECTED,
    SinkStatus.ERROR,
)


@dataclass(frozen=True)
class SinkState:
    """Snapshot of the Bluetooth receiver, safe to read from any thread."""

    status: SinkStatus = SinkStatus.OFF
    device_id: str = ""
    device_name: str = ""
    detail: str = ""
    error: str = ""
    since: float = 0.0

    @property
    def is_running(self) -> bool:
        return self.status in _ACTIVE_STATUSES

    @property
    def is_connected(self) -> bool:
        return self.status is SinkStatus.CONNECTED


def describe_state(state: SinkState) -> str:
    """One line for the settings dialog (no technical jargon when it works)."""
    if state.status is SinkStatus.OFF:
        return "Off — this PC is not acting as a Bluetooth speaker."
    if state.status is SinkStatus.STARTING:
        return state.detail or "Starting the Bluetooth audio receiver…"
    if state.status is SinkStatus.READY:
        return state.detail or "Waiting for a Bluetooth audio connection."
    if state.status is SinkStatus.CONNECTING:
        name = state.device_name or "the Bluetooth device"
        return f"Connecting to {name}…"
    if state.status is SinkStatus.CONNECTED:
        name = state.device_name or "the Bluetooth device"
        return f"Receiving audio from {name}."
    reason = state.error or state.detail or "unknown error"
    return f"Bluetooth error: {reason}"


class BluetoothSinkWorker:
    """Owns the WinRT connection and its asyncio loop.

    `backend_factory` exists so tests can run the same state machine without a
    Bluetooth radio; the production backend is `WinRtPlaybackBackend`.
    """

    def __init__(
        self,
        settings,
        *,
        log: logging.Logger | None = None,
        backend_factory: Callable[[logging.Logger], object] | None = None,
        on_state: Callable[[SinkState], None] | None = None,
    ):
        self._settings = settings
        self._log = log or logger
        self._backend_factory = backend_factory or WinRtPlaybackBackend
        self._on_state = on_state

        self._state = SinkState()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._stop_requested = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self._op_lock: asyncio.Lock | None = None
        self._op_task: asyncio.Task | None = None
        self._backend = None
        self._connection = None
        self._devices: tuple[BluetoothDeviceInfo, ...] = ()
        self._devices_error = ""
        self._initial_target: tuple[str, str] | None = None

    # -- public API -------------------------------------------------------- #
    @property
    def settings(self):
        return self._settings

    def state(self) -> SinkState:
        with self._state_lock:
            return self._state

    def devices(self) -> tuple[BluetoothDeviceInfo, ...]:
        return self._devices

    def devices_error(self) -> str:
        return self._devices_error

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, device_id: str | None = None, device_name: str = "") -> None:
        """Start the receiver thread; connect when a device is provided."""
        if self.is_running:
            if device_id:
                self.connect_now(device_id, device_name)
            return
        self._stop_requested.clear()
        self._loop_ready.clear()
        self._initial_target = (device_id, device_name) if device_id else None
        self._set_state(
            SinkStatus.STARTING, detail="Starting the Bluetooth audio receiver…"
        )
        self._thread = threading.Thread(
            target=self._thread_main, name="bark-bluetooth", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_requested.set()
        self._loop_ready.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:  # pragma: no cover - loop already closed
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():  # pragma: no cover - daemon thread, abandoned
                self._log.warning(
                    "the Bluetooth receiver thread did not stop within %.1fs", timeout
                )
        self._loop = None
        self._set_state(SinkStatus.OFF, detail="")

    def restart(self) -> None:
        self.stop()
        if self._settings.bluetooth_enabled:
            self.start()

    def configure(self, settings) -> None:
        """Apply saved settings; restart only when the Bluetooth setup changed."""
        previous, self._settings = self._settings, settings
        if not settings.bluetooth_enabled:
            self.stop()
            return
        if not previous.bluetooth_enabled:
            self.restart()
            return
        if not self.is_running:
            self.start()
            return
        if previous.bluetooth_device_id != settings.bluetooth_device_id:
            if settings.bluetooth_device_id:
                self.connect_now(
                    settings.bluetooth_device_id, settings.bluetooth_device_name
                )
            else:
                self.disconnect_now()

    def connect_now(self, device_id: str, device_name: str = "") -> None:
        """Connect from the UI, independent of the saved `enabled` flag."""
        device_id = (device_id or "").strip()
        if not device_id:
            return
        self._initial_target = (device_id, device_name)
        if not self.is_running:
            self.start(device_id, device_name)
            return
        self._post(self._connect, device_id, device_name)

    def disconnect_now(self) -> None:
        self._initial_target = None
        if self.is_running:
            self._post(self._disconnect_and_wait)

    # -- thread plumbing --------------------------------------------------- #
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:  # pragma: no cover - normal shutdown
            pass
        except Exception:  # pragma: no cover - unexpected WinRT/asyncio failure
            self._log.exception("the Bluetooth receiver crashed")
            self._set_state(
                SinkStatus.ERROR,
                error="The Bluetooth audio receiver stopped unexpectedly.",
            )
        finally:
            self._loop = None
            self._op_lock = None
            self._op_task = None
            self._loop_ready.set()
            if self._stop_requested.is_set():
                self._set_state(SinkStatus.OFF, detail="")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_event = asyncio.Event()
        self._op_lock = asyncio.Lock()
        self._loop_ready.set()
        if self._stop_requested.is_set():
            return

        if self._backend is None:
            try:
                self._backend = self._backend_factory(self._log)
            except Exception as exc:
                self._log.exception("could not create the Bluetooth backend")
                self._set_state(
                    SinkStatus.ERROR,
                    error=f"Bluetooth support is unavailable ({exc}).",
                )
                return

        try:
            self._devices = tuple(await self._backend.list_devices())
            self._devices_error = ""
        except BluetoothError as exc:
            self._devices = ()
            self._devices_error = str(exc)
            self._log.warning("bluetooth: %s", exc)
        except Exception as exc:  # pragma: no cover - OS specific
            self._devices = ()
            self._devices_error = str(exc)
            self._log.warning("bluetooth: could not list devices: %s", exc)

        target = self._initial_target
        if (
            target is None
            and self._settings.bluetooth_enabled
            and self._settings.bluetooth_device_id
        ):
            target = (
                self._settings.bluetooth_device_id,
                self._settings.bluetooth_device_name,
            )

        if target is not None:
            assert self._op_lock is not None
            async with self._op_lock:
                await self._connect(*target)
        else:
            if self._devices:
                detail = "Select a paired Bluetooth device and press Connect."
            elif self._devices_error:
                detail = self._devices_error
            else:
                detail = (
                    "No paired Bluetooth audio devices found. Pair the phone in "
                    "Windows Settings, then press Refresh."
                )
            self._set_state(SinkStatus.READY, detail=detail)

        assert self._stop_event is not None
        await self._stop_event.wait()
        await self._disconnect_internal()

    def _request_stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        task = self._op_task
        if task is not None:
            task.cancel()

    def _post(self, function, *args) -> None:
        if not self._loop_ready.wait(2.0):  # pragma: no cover - slow loop start
            self._log.warning("the Bluetooth loop is not ready; command ignored")
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._start_operation, function, args)
        except RuntimeError:  # pragma: no cover - loop closed
            pass

    def _start_operation(self, function, args) -> None:
        if self._op_lock is None:
            return
        self._op_task = asyncio.ensure_future(self._run_operation(function, *args))

    async def _run_operation(self, function, *args) -> None:
        assert self._op_lock is not None
        if self._stop_requested.is_set():
            return
        async with self._op_lock:
            try:
                await function(*args)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - unexpected backend failure
                self._log.exception("a Bluetooth operation failed")
            finally:
                self._op_task = None

    # -- connection -------------------------------------------------------- #
    async def _connect(self, device_id: str, device_name: str = "") -> None:
        if self._stop_requested.is_set():
            return
        if self._connection is not None:
            await self._disconnect_internal()
        name = device_name or self._name_for(device_id)
        self._set_state(
            SinkStatus.CONNECTING,
            device_id=device_id,
            device_name=name,
            detail=f"Connecting to {name or 'the Bluetooth device'}…",
        )
        assert self._backend is not None
        try:
            handle = await self._backend.open(device_id, self._on_remote_closed)
        except BluetoothError as exc:
            self._set_state(
                SinkStatus.ERROR,
                device_id=device_id,
                device_name=name,
                error=str(exc),
                detail=str(exc),
            )
            return
        except Exception as exc:  # pragma: no cover - OS specific
            self._log.exception("could not open the Bluetooth audio connection")
            self._set_state(
                SinkStatus.ERROR,
                device_id=device_id,
                device_name=name,
                error=f"Could not open the Bluetooth audio connection: {exc}",
            )
            return
        if self._stop_requested.is_set():
            try:
                await self._backend.close(handle)
            except Exception:  # pragma: no cover - teardown
                pass
            return
        self._connection = handle
        self._set_state(
            SinkStatus.CONNECTED,
            device_id=device_id,
            device_name=name,
            detail=f"Receiving audio from {name or 'the Bluetooth device'}.",
        )

    async def _disconnect_internal(self) -> None:
        handle, self._connection = self._connection, None
        if handle is None or self._backend is None:
            return
        try:
            await self._backend.close(handle)
        except Exception:  # pragma: no cover - Windows already released it
            self._log.debug("closing the Bluetooth connection failed", exc_info=True)

    async def _disconnect_and_wait(self) -> None:
        await self._disconnect_internal()
        if not self._stop_requested.is_set():
            self._set_state(SinkStatus.READY, detail="Disconnected.")

    def _on_remote_closed(self) -> None:
        """Called by the backend on a WinRT thread when the phone disconnects."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        try:
            loop.call_soon_threadsafe(self._remote_closed)
        except RuntimeError:  # pragma: no cover - loop closed
            pass

    def _remote_closed(self) -> None:
        self._connection = None
        if self._stop_requested.is_set():
            return
        self._set_state(
            SinkStatus.READY,
            detail="The Bluetooth device closed the connection. Press Connect to retry.",
        )

    def _name_for(self, device_id: str) -> str:
        for device in self._devices:
            if device.id == device_id:
                return device.name
        if device_id == self._settings.bluetooth_device_id:
            return self._settings.bluetooth_device_name
        return ""

    # -- state ------------------------------------------------------------- #
    def _set_state(
        self,
        status: SinkStatus,
        *,
        device_id: str | None = None,
        device_name: str | None = None,
        detail: str = "",
        error: str = "",
    ) -> None:
        with self._state_lock:
            previous = self._state
            self._state = SinkState(
                status=status,
                device_id=previous.device_id if device_id is None else device_id,
                device_name=previous.device_name if device_name is None else device_name,
                detail=detail,
                error=error,
                since=time.monotonic(),
            )
            state = self._state
        if status is not previous.status:
            self._log.info(
                "bluetooth: %s%s", status.value, f" ({detail})" if detail else ""
            )
        callback = self._on_state
        if callback is not None:
            try:
                callback(state)
            except Exception:  # pragma: no cover - callback isolation
                self._log.exception("the Bluetooth state callback failed")
