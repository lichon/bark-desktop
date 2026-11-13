"""USB adapter discovery helpers (no libusb context is opened)."""

from __future__ import annotations

from bark_pc.bluetooth import usb_adapters
from bark_pc.bluetooth.usb_adapters import (
    USB_BT_HCI_CLASS_TUPLE,
    UsbBluetoothAdapter,
    _adapters_from_candidates,
    _Candidate,
    _is_bluetooth_hci,
)


class _Setting:
    def __init__(self, class_, sub_class, protocol):
        self._class = class_
        self._sub_class = sub_class
        self._protocol = protocol

    def getClass(self):
        return self._class

    def getSubClass(self):
        return self._sub_class

    def getProtocol(self):
        return self._protocol


class _Interface:
    def __init__(self, settings):
        self._settings = settings

    def __iter__(self):
        return iter(self._settings)


class _Configuration:
    def __init__(self, interfaces):
        self._interfaces = interfaces

    def __iter__(self):
        return iter(self._interfaces)


class _Device:
    def __init__(self, class_, sub_class, protocol, configurations=()):
        self._class = class_
        self._sub_class = sub_class
        self._protocol = protocol
        self._configurations = configurations

    def getDeviceClass(self):
        return self._class

    def getDeviceSubClass(self):
        return self._sub_class

    def getDeviceProtocol(self):
        return self._protocol

    def __iter__(self):
        return iter(self._configurations)


def test_a_bluetooth_class_device_is_recognised():
    device = _Device(*USB_BT_HCI_CLASS_TUPLE)
    assert _is_bluetooth_hci(device) is True


def test_a_composite_device_with_a_bluetooth_interface_is_recognised():
    device = _Device(
        0x00,
        0x00,
        0x00,
        [
            _Configuration(
                [
                    _Interface([_Setting(0x03, 0x01, 0x01)]),
                    _Interface([_Setting(*USB_BT_HCI_CLASS_TUPLE)]),
                ]
            )
        ],
    )
    assert _is_bluetooth_hci(device) is True


def test_usb_drives_and_hubs_are_ignored():
    assert _is_bluetooth_hci(_Device(0x08, 0x06, 0x50)) is False
    assert _is_bluetooth_hci(_Device(0x09, 0x00, 0x00)) is False


def test_serial_numbers_make_the_selector_stable():
    adapters = _adapters_from_candidates(
        [
            _Candidate(0x0A12, 0x0001, 1, "1", "CSR8510 A10", "ABC123"),
            _Candidate(0x0A12, 0x0001, 1, "2", "CSR8510 A10", "ABC124"),
        ]
    )
    assert [adapter.selector for adapter in adapters] == [
        "usb:0A12:0001/ABC123",
        "usb:0A12:0001/ABC124",
    ]
    assert [adapter.name for adapter in adapters] == ["CSR8510 A10", "CSR8510 A10"]


def test_identical_dongles_without_serials_get_index_suffixes():
    adapters = _adapters_from_candidates(
        [
            _Candidate(0x0A12, 0x0001, 1, "1", "", ""),
            _Candidate(0x0A12, 0x0001, 1, "2", "", ""),
        ]
    )
    assert sorted(adapter.selector for adapter in adapters) == [
        "usb:0A12:0001#0",
        "usb:0A12:0001#1",
    ]
    assert all(adapter.name.startswith("CSR Bluetooth adapter 0A12:0001") for adapter in adapters)


def test_a_single_adapter_needs_no_suffix():
    adapters = _adapters_from_candidates(
        [_Candidate(0x0BDA, 0x8771, 2, "3", "Realtek Bluetooth", "")]
    )
    assert len(adapters) == 1
    assert adapters[0].selector == "usb:0BDA:8771"
    assert adapters[0].label == "Realtek Bluetooth (0BDA:8771)"


def test_adapter_values_are_immutable_snapshots():
    adapter = UsbBluetoothAdapter(
        selector="usb:0A12:0001",
        name="CSR8510 A10",
        vendor_id=0x0A12,
        product_id=0x0001,
    )
    assert adapter.vendor_product == "0A12:0001"
    assert adapter.label == "CSR8510 A10 (0A12:0001)"


def test_support_probe_never_raises():
    available, reason = usb_adapters.bluetooth_hci_support()
    assert isinstance(available, bool)
    assert isinstance(reason, str)


def test_listing_never_raises():
    adapters, error = usb_adapters.list_usb_bluetooth_adapters()
    assert isinstance(adapters, list)
    assert isinstance(error, str)
