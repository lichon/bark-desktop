"""USB Bluetooth adapters that Bumble can drive directly.

Bumble speaks HCI to a Bluetooth controller over libusb, which is what makes an
ANCS receiver possible on Windows without the Windows Bluetooth stack.  The
catch is that libusb (on Windows) needs a WinUSB/libusbK driver bound to the
dongle — usually with [Zadig](https://zadig.akeo.ie/).  This module only lists
candidate adapters; the `ancs_backend` module opens the selected one.

The selectors returned here use Bumble's transport syntax
(`usb:VVVV:PPPP`, `usb:VVVV:PPPP/SERIAL` or `usb:VVVV:PPPP#N`), so they can be
stored in settings and passed to `bumble.transport.open_transport()` unchanged.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("bark_pc.bluetooth.usb")

# USB class/subclass/protocol of a Bluetooth primary controller (bumble's
# `USB_BT_HCI_CLASS_TUPLE`).  Composite devices expose this on an interface
# instead of the device class, which is why `_is_bluetooth_hci` checks both.
USB_DEVICE_CLASS_DEVICE = 0x00
USB_DEVICE_CLASS_MISCELLANEOUS = 0xEF
USB_DEVICE_CLASS_WIRELESS_CONTROLLER = 0xE0
USB_DEVICE_SUBCLASS_RF_CONTROLLER = 0x01
USB_DEVICE_PROTOCOL_BLUETOOTH_PRIMARY_CONTROLLER = 0x01

USB_BT_HCI_CLASS_TUPLE = (
    USB_DEVICE_CLASS_WIRELESS_CONTROLLER,
    USB_DEVICE_SUBCLASS_RF_CONTROLLER,
    USB_DEVICE_PROTOCOL_BLUETOOTH_PRIMARY_CONTROLLER,
)

DRIVER_HINT = (
    "On Windows the adapter must use the WinUSB driver"
)

# Fallback names for when libusb cannot read the product string (no WinUSB driver
# yet).  A CSR8510-class BLE 4.0 dongle is the simplest choice for ANCS.
_VENDOR_NAMES = {
    0x0A12: "CSR",
    0x0A5C: "Broadcom",
    0x0BDA: "Realtek",
    0x0CF3: "Qualcomm",
    0x8087: "Intel",
}


@dataclass(frozen=True)
class UsbBluetoothAdapter:
    """One USB Bluetooth controller Bumble can try to open."""

    selector: str
    name: str
    vendor_id: int
    product_id: int
    bus: int = 0
    port: str = ""
    serial: str = ""

    @property
    def vendor_product(self) -> str:
        return f"{self.vendor_id:04X}:{self.product_id:04X}"

    @property
    def label(self) -> str:
        return f"{self.name} ({self.vendor_product})"


def bluetooth_hci_support() -> tuple[bool, str]:
    """Return `(available, reason)` without opening a USB device."""
    try:
        import bumble  # noqa: F401,PLC0415 - optional dependency, probed lazily
    except Exception as exc:  # pragma: no cover - depends on the installation
        return False, f"Bumble is not available ({exc})."
    try:
        import usb1  # noqa: F401,PLC0415 - optional dependency, probed lazily
        from bumble.transport.usb import load_libusb  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on the installation
        return False, f"libusb is not available ({exc})."
    try:
        load_libusb()
    except Exception as exc:  # pragma: no cover - platform/DLL specific
        return False, f"libusb could not be loaded ({exc})."
    return True, ""


def _is_bluetooth_hci(device: Any) -> bool:
    """True when a libusb device looks like a Bluetooth HCI controller."""
    try:
        if (
            device.getDeviceClass(),
            device.getDeviceSubClass(),
            device.getDeviceProtocol(),
        ) == USB_BT_HCI_CLASS_TUPLE:
            return True
        if device.getDeviceClass() not in (
            USB_DEVICE_CLASS_DEVICE,
            USB_DEVICE_CLASS_MISCELLANEOUS,
        ):
            return False
        for configuration in device:
            for interface in configuration:
                for setting in interface:
                    if (
                        setting.getClass(),
                        setting.getSubClass(),
                        setting.getProtocol(),
                    ) == USB_BT_HCI_CLASS_TUPLE:
                        return True
    except Exception:  # pragma: no cover - descriptors can be unreadable
        logger.debug("could not read USB device descriptors", exc_info=True)
    return False


def _port_path(device: Any) -> str:
    try:
        return "-".join(str(number) for number in device.getPortNumberList())
    except Exception:  # pragma: no cover - descriptor differences
        return ""


def _adapter_strings(device: Any) -> tuple[str, str]:
    """`(product, serial)`; both may be empty when libusb cannot open it."""
    product = serial = ""
    handle = None
    try:
        handle = device.open()
        product = (handle.getProduct() or "").strip()
        serial = (handle.getSerialNumber() or "").strip()
    except Exception:  # pragma: no cover - no WinUSB driver bound
        logger.debug("could not open the USB device for its strings", exc_info=True)
    finally:
        if handle is not None:
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort
                pass
    return product, serial


@dataclass
class _Candidate:
    vendor_id: int
    product_id: int
    bus: int
    port: str
    product: str
    serial: str


def _selector(candidate: _Candidate, index: int, count: int) -> str:
    """Build a Bumble transport selector for one candidate.

    Bumble resolves `usb:VVVV:PPPP` to the first matching device, so a serial
    number or `#N` suffix is needed when the same model is plugged in twice.
    """
    base = f"usb:{candidate.vendor_id:04X}:{candidate.product_id:04X}"
    if candidate.serial:
        return f"{base}/{candidate.serial}"
    if count > 1:
        return f"{base}#{index}"
    return base


def _adapters_from_candidates(
    candidates: list[_Candidate],
) -> list[UsbBluetoothAdapter]:
    counts: dict[tuple[int, int], int] = {}
    indexes: dict[tuple[int, int], int] = {}
    for candidate in candidates:
        key = (candidate.vendor_id, candidate.product_id)
        counts[key] = counts.get(key, 0) + 1

    adapters: list[UsbBluetoothAdapter] = []
    for candidate in candidates:
        key = (candidate.vendor_id, candidate.product_id)
        index = indexes.get(key, 0)
        indexes[key] = index + 1
        name = candidate.product or (
            f"{_VENDOR_NAMES.get(candidate.vendor_id, 'USB')} Bluetooth adapter "
            f"{candidate.vendor_id:04X}:{candidate.product_id:04X}"
        )
        adapters.append(
            UsbBluetoothAdapter(
                selector=_selector(candidate, index, counts[key]),
                name=name,
                vendor_id=candidate.vendor_id,
                product_id=candidate.product_id,
                bus=candidate.bus,
                port=candidate.port,
                serial=candidate.serial,
            )
        )
    adapters.sort(key=lambda adapter: (adapter.name.casefold(), adapter.bus, adapter.port))
    return adapters


def list_usb_bluetooth_adapters() -> tuple[list[UsbBluetoothAdapter], str]:
    """Enumerate Bluetooth HCI USB adapters.  Never raises.

    Returns `(adapters, error_message)`.  An empty list with an error means
    libusb/Bumble is missing; an empty list without an error means no Bluetooth
    USB controller is plugged in (or its descriptors could not be read).
    """
    supported, reason = bluetooth_hci_support()
    if not supported:
        return [], reason

    import usb1  # noqa: PLC0415 - Windows-only dependency, imported lazily

    context = None
    try:
        context = usb1.USBContext()
        context.open()
        candidates: list[_Candidate] = []
        for device in context.getDeviceIterator(skip_on_error=True):
            try:
                if not _is_bluetooth_hci(device):
                    continue
                product, serial = _adapter_strings(device)
                candidates.append(
                    _Candidate(
                        vendor_id=int(device.getVendorID()),
                        product_id=int(device.getProductID()),
                        bus=int(device.getBusNumber()),
                        port=_port_path(device),
                        product=product,
                        serial=serial,
                    )
                )
            except Exception:  # pragma: no cover - device disappeared
                logger.debug("could not inspect a USB device", exc_info=True)
            finally:
                try:
                    device.close()
                except Exception:  # pragma: no cover - best effort
                    pass
        return _adapters_from_candidates(candidates), ""
    except Exception as exc:  # pragma: no cover - libusb/driver specific
        logger.debug("could not enumerate USB devices", exc_info=True)
        return [], f"Could not list USB Bluetooth adapters: {exc}. {DRIVER_HINT}"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:  # pragma: no cover - best effort
                pass


def current_platform_note() -> str:
    """A short note about the driver requirement, shown in the settings dialog."""
    if sys.platform == "win32":
        return DRIVER_HINT
    if sys.platform == "darwin":
        return "Bumble needs a USB Bluetooth dongle that libusb can open on macOS."
    return "Bumble needs permission to open the USB Bluetooth dongle (udev rules on Linux)."
