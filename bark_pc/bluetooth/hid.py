"""HID over GATT (HOGP, service 0x1812) as the accessory's "front door".

iOS treats a bonded BLE keyboard as a first-class accessory: it appears in
Settings → Bluetooth, it survives address rotation, and the link it brings up is
exactly the encrypted, bonded connection ANCS needs.  That is why advertising
*and* serving HID is the community-verified way to get an iPhone to list an
accessory at all — a lone 0x15 ANCS solicitation is enough for the notification
handshake but iOS may never show the device.

Nothing here emulates a real keyboard for the user: the service exists so iOS
sees a valid boot keyboard (report map, input/output reports, protocol mode).
`HidKeyboard.type_text()` can still send keystrokes, which is handy when iOS
asks the accessory to type a pairing code.

Bumble is imported lazily: the report map, the key mapping and the report bytes
are plain data and stay testable without it (the app itself imports this module
only when the adapter opens).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

try:  # pragma: no cover - exercised through the importorskip tests
    from bumble import gatt
except Exception:  # pragma: no cover - Bumble is an optional dependency
    gatt = None

logger = logging.getLogger("bark_pc.hid")

HID_SERVICE_UUID_16 = 0x1812
# Assigned Numbers, HID category (0x0F): 0x03C0 generic, 0x03C1 keyboard.
HID_APPEARANCE_GENERIC = 0x03C0
HID_APPEARANCE_KEYBOARD = 0x03C1

REPORT_ID_KEYBOARD = 0x01
REPORT_ID_LEDS = 0x01
REPORT_TYPE_INPUT = 0x01
REPORT_TYPE_OUTPUT = 0x02
INPUT_REPORT_SIZE = 8
OUTPUT_REPORT_SIZE = 1

MODIFIER_LEFT_CTRL = 0x01
MODIFIER_LEFT_SHIFT = 0x02

# HID Information: bcdHID 1.11, country code 0, flags (normally connectable).
# Four octets - there is no bcdDevice here, that belongs to USB HID.
HID_INFORMATION = bytes([0x11, 0x01, 0x00, 0x02])

# Boot-keyboard report map: 1 modifier byte, 1 reserved byte, 6 key slots in,
# 1 LED byte out.  Report ID 1 for both directions.
# fmt: off
HID_REPORT_MAP = bytes(
    [
        0x05, 0x01,  # Usage Page (Generic Desktop)
        0x09, 0x06,  # Usage (Keyboard)
        0xA1, 0x01,  # Collection (Application)
        0x85, REPORT_ID_KEYBOARD,  #   Report ID (1)
        0x05, 0x07,  #   Usage Page (Keyboard/Keypad)
        0x19, 0xE0,  #   Usage Minimum (Left Control)
        0x29, 0xE7,  #   Usage Maximum (Right GUI)
        0x15, 0x00,  #   Logical Minimum (0)
        0x25, 0x01,  #   Logical Maximum (1)
        0x75, 0x01,  #   Report Size (1)
        0x95, 0x08,  #   Report Count (8)
        0x81, 0x02,  #   Input (Data,Variable,Absolute)
        0x95, 0x01,  #   Report Count (1)
        0x75, 0x08,  #   Report Size (8)
        0x81, 0x01,  #   Input (Constant)
        0x95, 0x05,  #   Report Count (5)
        0x75, 0x01,  #   Report Size (1)
        0x05, 0x08,  #   Usage Page (LEDs)
        0x19, 0x01,  #   Usage Minimum (Num Lock)
        0x29, 0x05,  #   Usage Maximum (Kana)
        0x91, 0x02,  #   Output (Data,Variable,Absolute)
        0x95, 0x01,  #   Report Count (1)
        0x75, 0x03,  #   Report Size (3)
        0x91, 0x01,  #   Output (Constant)
        0x95, 0x06,  #   Report Count (6)
        0x75, 0x08,  #   Report Size (8)
        0x15, 0x00,  #   Logical Minimum (0)
        0x25, 0x65,  #   Logical Maximum (101)
        0x05, 0x07,  #   Usage Page (Keyboard/Keypad)
        0x19, 0x00,  #   Usage Minimum (Reserved)
        0x29, 0x65,  #   Usage Maximum (Keyboard Application)
        0x81, 0x00,  #   Input (Data,Array,Absolute)
        0xC0,  # End Collection
    ]
)
# fmt: on

_UNSHIFTED = {
    " ": 0x2C,
    "\n": 0x28,
    "\r": 0x28,
    "\t": 0x2B,
    "'": 0x34,
    ",": 0x36,
    "-": 0x2D,
    ".": 0x37,
    "/": 0x38,
    ";": 0x33,
    "=": 0x2E,
    "[": 0x2F,
    "\\": 0x31,
    "]": 0x30,
    "`": 0x35,
}

_SHIFTED = {
    "!": 0x1E,
    '"': 0x34,
    "#": 0x20,
    "$": 0x21,
    "%": 0x22,
    "&": 0x24,
    "(": 0x26,
    ")": 0x27,
    "*": 0x25,
    "+": 0x2E,
    ":": 0x33,
    "<": 0x36,
    ">": 0x37,
    "?": 0x38,
    "@": 0x1F,
    "^": 0x23,
    "_": 0x2D,
    "{": 0x2F,
    "|": 0x31,
    "}": 0x30,
    "~": 0x35,
}


def character_to_key(character: str) -> tuple[int, int] | None:
    """Map one character to (modifiers, HID keycode), or None when unsupported."""
    if len(character) != 1:
        return None
    if "a" <= character <= "z":
        return 0x00, 0x04 + ord(character) - ord("a")
    if "A" <= character <= "Z":
        return MODIFIER_LEFT_SHIFT, 0x04 + ord(character) - ord("A")
    if "1" <= character <= "9":
        return 0x00, 0x1E + ord(character) - ord("1")
    if character == "0":
        return 0x00, 0x27
    if character in _UNSHIFTED:
        return 0x00, _UNSHIFTED[character]
    if character in _SHIFTED:
        return MODIFIER_LEFT_SHIFT, _SHIFTED[character]
    return None


def input_report(character: str) -> bytes | None:
    """The 8-byte boot-keyboard input report that presses *character*."""
    mapped = character_to_key(character)
    if mapped is None:
        return None
    modifiers, keycode = mapped
    return bytes([modifiers, 0x00, keycode, 0x00, 0x00, 0x00, 0x00, 0x00])


def typed_reports(text: str) -> list[bytes]:
    """Reports for *text*, alternating each key press with a key release."""
    reports: list[bytes] = []
    for character in text:
        pressed = input_report(character)
        if pressed is None:
            logger.debug("hid: no keycode for %r", character)
            continue
        reports.append(pressed)
        reports.append(bytes(INPUT_REPORT_SIZE))
    return reports


if gatt is not None:

    class HidKeyboard(gatt.TemplateService):
        """A minimal HOGP keyboard service (service UUID 0x1812)."""

        UUID = gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE

        def __init__(
            self,
            *,
            manufacturer: str = "Bark Desktop",
            on_output_report=None,
        ) -> None:
            self.manufacturer = manufacturer
            self.on_output_report = on_output_report
            self._protocol_mode = 1  # 1 = report protocol, 0 = boot protocol
            self._leds = 0

            self.hid_information = gatt.Characteristic(
                gatt.GATT_HID_INFORMATION_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                HID_INFORMATION,
            )
            self.report_map = gatt.Characteristic(
                gatt.GATT_REPORT_MAP_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ,
                gatt.Characteristic.READABLE,
                HID_REPORT_MAP,
            )
            self.hid_control_point = gatt.Characteristic(
                gatt.GATT_HID_CONTROL_POINT_CHARACTERISTIC,
                gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.WRITEABLE,
            )
            self.protocol_mode = gatt.Characteristic(
                gatt.GATT_PROTOCOL_MODE_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ
                | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
                gatt.CharacteristicValue(
                    read=lambda _connection: bytes([self._protocol_mode]),
                    write=self._write_protocol_mode,
                ),
            )
            self.input_report = gatt.Characteristic(
                gatt.GATT_REPORT_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ | gatt.Characteristic.Properties.NOTIFY,
                gatt.Characteristic.READABLE,
                bytes(INPUT_REPORT_SIZE),
                descriptors=[
                    gatt.Descriptor(
                        gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                        gatt.Characteristic.READABLE,
                        bytes([REPORT_ID_KEYBOARD, REPORT_TYPE_INPUT]),
                    )
                ],
            )
            self.output_report = gatt.Characteristic(
                gatt.GATT_REPORT_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ
                | gatt.Characteristic.Properties.WRITE
                | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
                gatt.CharacteristicValue(
                    read=lambda _connection: bytes([self._leds]),
                    write=self._write_leds,
                ),
                descriptors=[
                    gatt.Descriptor(
                        gatt.GATT_REPORT_REFERENCE_DESCRIPTOR,
                        gatt.Characteristic.READABLE,
                        bytes([REPORT_ID_LEDS, REPORT_TYPE_OUTPUT]),
                    )
                ],
            )
            self.boot_keyboard_input = gatt.Characteristic(
                gatt.GATT_BOOT_KEYBOARD_INPUT_REPORT_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ | gatt.Characteristic.Properties.NOTIFY,
                gatt.Characteristic.READABLE,
                bytes(INPUT_REPORT_SIZE),
            )
            self.boot_keyboard_output = gatt.Characteristic(
                gatt.GATT_BOOT_KEYBOARD_OUTPUT_REPORT_CHARACTERISTIC,
                gatt.Characteristic.Properties.READ
                | gatt.Characteristic.Properties.WRITE
                | gatt.Characteristic.Properties.WRITE_WITHOUT_RESPONSE,
                gatt.Characteristic.READABLE | gatt.Characteristic.WRITEABLE,
                gatt.CharacteristicValue(
                    read=lambda _connection: bytes([self._leds]),
                    write=self._write_leds,
                ),
            )

            super().__init__(
                [
                    self.hid_information,
                    self.report_map,
                    self.hid_control_point,
                    self.protocol_mode,
                    self.input_report,
                    self.output_report,
                    self.boot_keyboard_input,
                    self.boot_keyboard_output,
                ]
            )

        # -- state ---------------------------------------------------------- #
        @property
        def protocol_mode_is_report(self) -> bool:
            return self._protocol_mode == 1

        @property
        def leds(self) -> int:
            return self._leds

        def _write_protocol_mode(self, _connection, value: bytes) -> None:
            if value and value[0] in (0x00, 0x01):
                self._protocol_mode = value[0]

        def _write_leds(self, _connection, value: bytes) -> None:
            self._leds = value[0] if value else 0
            logger.info("hid: keyboard LEDs 0x%02X", self._leds)
            if self.on_output_report is not None:
                try:
                    self.on_output_report(self._leds)
                except Exception:  # pragma: no cover - callback isolation
                    logger.debug("hid: output-report callback failed", exc_info=True)

        # -- sending -------------------------------------------------------- #
        async def send_report(self, device, report: bytes) -> None:
            """Push one input report to the host (no-op when nothing subscribed)."""
            report = bytes(report)
            self.input_report.value = report
            self.boot_keyboard_input.value = report
            await device.notify_subscribers(self.input_report)
            await device.notify_subscribers(self.boot_keyboard_input)

        async def type_text(self, device, text: str, *, delay: float = 0.02) -> int:
            """Type *text* as a boot keyboard; returns the input reports sent."""
            sent = 0
            for report in typed_reports(text):
                await self.send_report(device, report)
                sent += 1
                await asyncio.sleep(delay)
            return sent

else:  # pragma: no cover - only when Bumble is not installed

    class HidKeyboard:  # type: ignore[no-redef]
        """Placeholder that explains the missing dependency."""

        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError(
                "Bumble is required for the HID over GATT service; install the "
                "bluetooth extras to enable it."
            )


def hid_service_uuids() -> Sequence[int]:
    """16-bit UUIDs the HID service needs in the advertising packet."""
    return (HID_SERVICE_UUID_16,)
