"""Bluetooth audio receiving (A2DP sink) through the Windows Bluetooth stack.

Since Windows 10 2004 the OS can act as a Bluetooth A2DP *sink*, but a program
has to enable and open the connection.  This package wraps
`Windows.Media.Audio.AudioPlaybackConnection` (the same API used by the
AudioPlaybackConnector project) so a paired phone can stream audio to the PC,
which plays it on the current Windows output device.  No codec, audio decoder or
dedicated Bluetooth adapter is involved: Windows does all of that.
"""

from __future__ import annotations

from .sink import (
    SINK_STATUS_LABELS,
    BluetoothSinkWorker,
    SinkState,
    SinkStatus,
    describe_state,
)
from .winrt_backend import (
    BluetoothDeviceInfo,
    BluetoothError,
    bluetooth_support,
    list_sink_devices,
    list_sink_devices_sync,
)

__all__ = [
    "BluetoothDeviceInfo",
    "BluetoothError",
    "BluetoothSinkWorker",
    "SINK_STATUS_LABELS",
    "SinkState",
    "SinkStatus",
    "bluetooth_support",
    "describe_state",
    "list_sink_devices",
    "list_sink_devices_sync",
]
