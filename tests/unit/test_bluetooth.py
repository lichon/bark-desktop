"""Bluetooth audio receiver: state machine, settings, and failure handling.

No test opens a real A2DP connection: the WinRT backend is replaced by a fake
that only records calls.  The Windows API itself is covered by the manual
checklist in docs/validation/Bluetooth.md.
"""

from __future__ import annotations

import asyncio
import time

from bark_pc.bluetooth import (
    BluetoothDeviceInfo,
    BluetoothError,
    BluetoothSinkWorker,
    SinkState,
    SinkStatus,
    describe_state,
)
from bark_pc.bluetooth.winrt_backend import WinRtPlaybackBackend
from bark_pc.core.config import Settings


class FakeBackend:
    def __init__(self, devices=None, fail_with: str | None = None):
        self.devices = list(
            devices
            if devices is not None
            else [BluetoothDeviceInfo("device-1", "Pixel 9")]
        )
        self.fail_with = fail_with
        self.open_calls: list[str] = []
        self.closed: list[object] = []
        self.remote_close = None
        self.list_error: str | None = None

    async def list_devices(self):
        if self.list_error:
            raise BluetoothError(self.list_error)
        return list(self.devices)

    async def open(self, device_id, on_remote_close):
        if self.fail_with:
            raise BluetoothError(self.fail_with)
        self.open_calls.append(device_id)
        self.remote_close = on_remote_close
        return {"device_id": device_id}

    async def close(self, handle):
        self.closed.append(handle)


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_worker(settings: Settings, backend: FakeBackend) -> BluetoothSinkWorker:
    return BluetoothSinkWorker(settings, backend_factory=lambda _log: backend)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_bluetooth_is_off_by_default(settings_store):
    settings = settings_store.load()
    assert settings.bluetooth_enabled is False
    assert settings.bluetooth_device_id == ""
    assert settings.bluetooth_device_name == ""


def test_bluetooth_values_survive_a_settings_round_trip(portable_paths):
    from bark_pc.core.config import SettingsStore

    store = SettingsStore(portable_paths)
    store.update(
        bluetooth_enabled=True,
        bluetooth_device_id="Bluetooth#Bluetooth00:11:22:33:44:55",
        bluetooth_device_name="  Pixel 9  ",
    )
    loaded = SettingsStore(portable_paths).load()
    assert loaded.bluetooth_enabled is True
    assert loaded.bluetooth_device_id == "Bluetooth#Bluetooth00:11:22:33:44:55"
    assert loaded.bluetooth_device_name == "Pixel 9"


def test_invalid_bluetooth_booleans_and_long_names_fall_back(portable_paths):
    from bark_pc.core.config import SettingsStore

    portable_paths.settings_path.write_text(
        '{"bluetooth_enabled": "yes", "bluetooth_device_name": "%s"}'
        % ("x" * 200),
        encoding="utf-8",
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.bluetooth_enabled is False
    assert settings.bluetooth_device_name == ""
    assert any("bluetooth" in warning for warning in settings.warnings)


# --------------------------------------------------------------------------- #
# Status descriptions
# --------------------------------------------------------------------------- #
def test_describe_state_covers_every_status():
    assert "Off" in describe_state(SinkState())
    assert "Starting" in describe_state(
        SinkState(status=SinkStatus.STARTING, detail="Starting the receiver…")
    )
    assert "Waiting" in describe_state(SinkState(status=SinkStatus.READY))
    assert "Pixel 9" in describe_state(
        SinkState(status=SinkStatus.CONNECTING, device_name="Pixel 9")
    )
    assert "Receiving audio from Pixel 9" in describe_state(
        SinkState(status=SinkStatus.CONNECTED, device_name="Pixel 9")
    )
    error = describe_state(
        SinkState(status=SinkStatus.ERROR, error="The request timed out.")
    )
    assert "Bluetooth error" in error and "timed out" in error


# --------------------------------------------------------------------------- #
# Worker lifecycle
# --------------------------------------------------------------------------- #
def test_worker_connects_to_the_saved_device():
    settings = Settings(
        bluetooth_enabled=True,
        bluetooth_device_id="device-1",
        bluetooth_device_name="Pixel 9",
    )
    backend = FakeBackend()
    worker = make_worker(settings, backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)
        assert backend.open_calls == ["device-1"]
        state = worker.state()
        assert state.device_name == "Pixel 9"
        assert state.is_connected
    finally:
        worker.stop()
    assert worker.state().status is SinkStatus.OFF
    assert backend.closed


def test_worker_disconnect_keeps_the_thread_alive():
    settings = Settings(
        bluetooth_enabled=True,
        bluetooth_device_id="device-1",
        bluetooth_device_name="Pixel 9",
    )
    backend = FakeBackend()
    worker = make_worker(settings, backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)
        worker.disconnect_now()
        assert wait_for(lambda: worker.state().status is SinkStatus.READY)
        assert backend.closed
        assert worker.is_running
    finally:
        worker.stop()


def test_worker_reports_a_connect_failure_and_stays_available():
    settings = Settings(
        bluetooth_enabled=True,
        bluetooth_device_id="device-1",
        bluetooth_device_name="Pixel 9",
    )
    backend = FakeBackend(fail_with="The request timed out.")
    worker = make_worker(settings, backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.ERROR)
        assert "timed out" in worker.state().error
        assert worker.is_running
    finally:
        worker.stop()


def test_worker_reports_a_device_that_closes_the_connection():
    settings = Settings(
        bluetooth_enabled=True,
        bluetooth_device_id="device-1",
        bluetooth_device_name="Pixel 9",
    )
    backend = FakeBackend()
    worker = make_worker(settings, backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)
        assert backend.remote_close is not None
        backend.remote_close()
        assert wait_for(
            lambda: worker.state().status is SinkStatus.READY
            and "closed the connection" in worker.state().detail
        )
        assert worker.is_running
    finally:
        worker.stop()


def test_worker_can_connect_without_the_enabled_flag():
    backend = FakeBackend()
    worker = make_worker(Settings(), backend)
    try:
        worker.connect_now("device-1", "Pixel 9")
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)
        assert backend.open_calls == ["device-1"]
    finally:
        worker.stop()


def test_configure_starts_and_stops_the_worker():
    backend = FakeBackend()
    worker = make_worker(Settings(), backend)
    try:
        worker.configure(
            Settings(
                bluetooth_enabled=True,
                bluetooth_device_id="device-1",
                bluetooth_device_name="Pixel 9",
            )
        )
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)

        worker.configure(Settings())
        assert wait_for(lambda: not worker.is_running)
        assert worker.state().status is SinkStatus.OFF
    finally:
        worker.stop()


def test_worker_keeps_running_when_device_enumeration_fails():
    settings = Settings(
        bluetooth_enabled=True,
        bluetooth_device_id="device-1",
        bluetooth_device_name="Pixel 9",
    )
    backend = FakeBackend()
    backend.list_error = "Could not list paired Bluetooth devices: access denied"
    worker = make_worker(settings, backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.CONNECTED)
        assert "access denied" in worker.devices_error()
    finally:
        worker.stop()


def test_winrt_backend_refuses_an_unknown_device_id():
    """A stale id must never reach `TryCreateFromId` (it segfaults the process)."""

    class Backend(WinRtPlaybackBackend):
        async def list_devices(self):
            return [BluetoothDeviceInfo("known-device", "Pixel 9")]

    backend = Backend()
    try:
        asyncio.run(backend.open("stale-device", lambda: None))
    except BluetoothError as exc:
        assert "not currently available" in str(exc)
    else:  # pragma: no cover - the guard must raise
        raise AssertionError("the backend accepted a device that was not enumerated")


def test_worker_ready_state_explains_how_to_pair_when_no_devices_exist():
    backend = FakeBackend(devices=[])
    worker = make_worker(Settings(bluetooth_enabled=True), backend)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is SinkStatus.READY)
        assert "Windows Settings" in worker.state().detail
    finally:
        worker.stop()
