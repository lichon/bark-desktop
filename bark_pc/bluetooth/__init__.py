"""Bluetooth helpers: a Windows A2DP sink and an ANCS receiver.

The A2DP part wraps `Windows.Media.Audio.AudioPlaybackConnection` so a paired
phone can stream audio to the PC, which plays it on the current Windows output
device.  No codec, audio decoder or dedicated Bluetooth adapter is involved:
Windows does all of that.

The ANCS part is different: it uses Bumble to drive a dedicated USB Bluetooth
dongle directly, then scans for the iPhone and connects to it as a BLE central
(the default), or advertises a small accessory when that direction is preferred
(see `ancs.py`).  Both features are independent and can be on at the same time.
"""

from __future__ import annotations

from .ancs import (
    ANCS_ADVERTISED_NAME,
    ANCS_APPEARANCE,
    ANCS_STATUS_LABELS,
    AncsError,
    AncsNotification,
    AncsPeerInfo,
    AncsState,
    AncsStatus,
    AncsWorker,
    describe_ancs_state,
)
from .sink import (
    SINK_STATUS_LABELS,
    BluetoothSinkWorker,
    SinkState,
    SinkStatus,
    describe_state,
)
from .usb_adapters import (
    UsbBluetoothAdapter,
    bluetooth_hci_support,
    current_platform_note,
    list_usb_bluetooth_adapters,
)
from .winrt_backend import (
    BluetoothDeviceInfo,
    BluetoothError,
    bluetooth_support,
    list_sink_devices,
    list_sink_devices_sync,
)

__all__ = [
    "ANCS_ADVERTISED_NAME",
    "ANCS_APPEARANCE",
    "ANCS_STATUS_LABELS",
    "AncsError",
    "AncsNotification",
    "AncsPeerInfo",
    "AncsState",
    "AncsStatus",
    "AncsWorker",
    "BluetoothDeviceInfo",
    "BluetoothError",
    "BluetoothSinkWorker",
    "SINK_STATUS_LABELS",
    "SinkState",
    "SinkStatus",
    "UsbBluetoothAdapter",
    "bluetooth_hci_support",
    "bluetooth_support",
    "current_platform_note",
    "describe_ancs_state",
    "describe_state",
    "list_sink_devices",
    "list_sink_devices_sync",
    "list_usb_bluetooth_adapters",
]
