"""Thin wrapper around `Windows.Media.Audio.AudioPlaybackConnection`.

The WinRT projection is imported lazily so this module can be imported (and the
worker unit-tested) on every platform.  The flow mirrors the reference
AudioPlaybackConnector project:

1. enumerate paired devices that expose an A2DP sink interface,
2. `AudioPlaybackConnection.TryCreateFromId(device_id)`,
3. `StartAsync()` (enable the connection) and `OpenAsync()` (connect),
4. watch `StateChanged` for a remote close, and `Close()` to release it.

Windows receives the audio and routes it to the current output endpoint; this
module never sees PCM samples.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger("bark_pc.bluetooth")

# Bluetooth A2DP sink support was added to Windows 10 version 2004 (build 19041).
MIN_WINDOWS_BUILD = 19041

_DEFAULT_DEVICE_NAME = "Bluetooth audio device"


class BluetoothError(RuntimeError):
    """A user-facing Bluetooth problem (pairing, driver, connection)."""


@dataclass(frozen=True)
class BluetoothDeviceInfo:
    """One paired device that can act as an A2DP source."""

    id: str
    name: str

    @property
    def label(self) -> str:
        return self.name or _DEFAULT_DEVICE_NAME


# --------------------------------------------------------------------------- #
# Lazy WinRT imports
# --------------------------------------------------------------------------- #
def _media_audio():
    from winrt.windows.media.audio import (  # noqa: PLC0415 - Windows only
        AudioPlaybackConnection,
        AudioPlaybackConnectionOpenResultStatus,
        AudioPlaybackConnectionState,
    )

    return (
        AudioPlaybackConnection,
        AudioPlaybackConnectionOpenResultStatus,
        AudioPlaybackConnectionState,
    )


def _device_information():
    from winrt.windows.devices.enumeration import DeviceInformation  # noqa: PLC0415

    return DeviceInformation


# --------------------------------------------------------------------------- #
# Capability probe
# --------------------------------------------------------------------------- #
def bluetooth_support() -> tuple[bool, str]:
    """Return `(available, reason)` without touching the Bluetooth radio."""
    if sys.platform != "win32":
        return False, "Receiving Bluetooth audio is only supported on Windows."
    try:
        build = sys.getwindowsversion().build
    except (AttributeError, OSError):  # pragma: no cover - defensive
        build = 0
    if build and build < MIN_WINDOWS_BUILD:
        return (
            False,
            "Windows 10 version 2004 (build 19041) or later is required "
            "for Bluetooth audio receiving.",
        )
    try:
        _media_audio()
        _device_information()
    except Exception as exc:  # pragma: no cover - depends on the installation
        return False, f"The Windows Bluetooth audio API is unavailable ({exc})."
    return True, ""


# --------------------------------------------------------------------------- #
# Device enumeration
# --------------------------------------------------------------------------- #
async def list_sink_devices() -> list[BluetoothDeviceInfo]:
    """List paired devices that Windows can use as an A2DP source.

    The selector comes from `AudioPlaybackConnection.GetDeviceSelector()` and
    only reports devices with an *enabled* A2DP sink interface, which is why a
    phone must be paired in Windows Settings first.
    """
    AudioPlaybackConnection, _, _ = _media_audio()
    DeviceInformation = _device_information()
    selector = AudioPlaybackConnection.get_device_selector()
    try:
        found = await DeviceInformation.find_all_async_aqs_filter(selector)
    except Exception as exc:
        raise BluetoothError(f"Could not list paired Bluetooth devices: {exc}") from exc

    devices: list[BluetoothDeviceInfo] = []
    for info in found:
        try:
            device_id = str(info.id or "")
            name = str(info.name or "").strip()
        except Exception:  # pragma: no cover - device disappeared mid-enumeration
            continue
        if not device_id:
            continue
        devices.append(BluetoothDeviceInfo(id=device_id, name=name))
    devices.sort(key=lambda device: device.label.casefold())
    return devices


def list_sink_devices_sync(timeout: float = 8.0) -> tuple[list[BluetoothDeviceInfo], str]:
    """Blocking variant for the Tk thread: `(devices, error_message)`."""

    async def collect() -> list[BluetoothDeviceInfo]:
        return await asyncio.wait_for(list_sink_devices(), timeout)

    try:
        return asyncio.run(collect()), ""
    except BluetoothError as exc:
        return [], str(exc)
    except asyncio.TimeoutError:
        return [], "Windows did not answer while listing Bluetooth devices."
    except Exception as exc:  # pragma: no cover - WinRT failures are OS specific
        logger.debug("could not list Bluetooth sink devices", exc_info=True)
        return [], f"Could not list paired Bluetooth devices: {exc}"


# --------------------------------------------------------------------------- #
# Connection wrapper
# --------------------------------------------------------------------------- #
class _OpenConnection:
    """One `AudioPlaybackConnection` plus the bookkeeping around it."""

    def __init__(self, connection, on_remote_close, log: logging.Logger):
        self.connection = connection
        self.on_remote_close = on_remote_close
        self.log = log
        self.token: object | None = None
        self.local_close = False

    def on_state_changed(self, sender, _args) -> None:
        _, _, AudioPlaybackConnectionState = _media_audio()
        try:
            state = sender.state
        except Exception:  # pragma: no cover - object already released
            return
        if state != AudioPlaybackConnectionState.CLOSED or self.local_close:
            return
        self.log.info("bluetooth: the remote device closed the audio connection")
        try:
            self.on_remote_close()
        except Exception:  # pragma: no cover - callback isolation
            self.log.exception("bluetooth close callback failed")

    def detach(self) -> None:
        if self.token is None:
            return
        try:
            self.connection.remove_state_changed(self.token)
        except Exception:  # pragma: no cover - already closed
            pass
        self.token = None


class WinRtPlaybackBackend:
    """Enable/open/close one A2DP sink connection at a time."""

    def __init__(self, log: logging.Logger | None = None):
        self.log = log or logger

    async def list_devices(self) -> list[BluetoothDeviceInfo]:
        return await list_sink_devices()

    async def open(self, device_id: str, on_remote_close) -> _OpenConnection:
        # `TryCreateFromId` crashes the process for an id that Windows does not
        # currently report (a stale device saved in settings, for example), so
        # never hand it anything that did not just come out of enumeration.
        devices = await self.list_devices()
        if all(device.id != device_id for device in devices):
            raise BluetoothError(
                "This Bluetooth device is not currently available. Pair it in "
                "Windows Settings and press Refresh."
            )
        AudioPlaybackConnection, status_enum, _ = _media_audio()
        try:
            connection = AudioPlaybackConnection.try_create_from_id(device_id)
        except Exception as exc:
            raise BluetoothError(
                "Windows could not create a Bluetooth audio connection for this "
                f"device: {exc}"
            ) from exc
        if connection is None:
            raise BluetoothError(
                "Windows could not create a Bluetooth audio connection. Pair the "
                "device in Windows Settings and make sure it supports audio playback."
            )

        handle = _OpenConnection(connection, on_remote_close, self.log)
        try:
            handle.token = connection.add_state_changed(handle.on_state_changed)
            await connection.start_async()
            result = await connection.open_async()
        except Exception as exc:
            handle.local_close = True
            handle.detach()
            try:
                connection.close()
            except Exception:  # pragma: no cover - best effort
                pass
            raise BluetoothError(f"Could not open the Bluetooth audio connection: {exc}") from exc

        status = result.status
        if status != status_enum.SUCCESS:
            handle.local_close = True
            handle.detach()
            try:
                connection.close()
            except Exception:  # pragma: no cover - best effort
                pass
            raise BluetoothError(_open_status_message(status, result, status_enum))
        return handle

    async def close(self, handle: _OpenConnection) -> None:
        handle.local_close = True
        handle.detach()
        try:
            handle.connection.close()
        except Exception:  # pragma: no cover - Windows already released it
            self.log.debug("closing the Bluetooth audio connection failed", exc_info=True)


def _open_status_message(status, result, status_enum) -> str:
    messages = {
        status_enum.REQUEST_TIMED_OUT: "The connection request timed out.",
        status_enum.DENIED_BY_SYSTEM: "Windows denied the Bluetooth audio connection.",
        status_enum.UNKNOWN_FAILURE: "Windows could not open the Bluetooth audio connection.",
    }
    message = messages.get(status, "Windows could not open the Bluetooth audio connection.")
    extended = getattr(result, "extended_error", None)
    if extended:
        try:
            message = f"{message} (0x{int(extended) & 0xFFFFFFFF:08X})"
        except (TypeError, ValueError):  # pragma: no cover - projection differences
            message = f"{message} ({extended})"
    return message
