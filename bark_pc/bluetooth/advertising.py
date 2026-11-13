"""Decode BLE advertising data, element by element, without Bumble.

The ANCS accessory only shows up on an iPhone when the advertising packet
carries an **0x15** (128-bit Service Solicitation) element holding Apple's ANCS
UUID — a plain 0x06/0x07 UUID list is not enough, and an appearance value on its
own never is.  This module is deliberately free of Bumble imports so the
diagnostic CLI (`ancs_connect --dump-advertising`) and the unit tests can inspect
a real packet on any machine, dongle or not.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Apple Notification Center Service (180D is the 16-bit Apple notification
# service id; 1815 is the "ANCS" alias used by some stacks).  On air the
# 128-bit form is little-endian, hence the byte sequence below.
ANCS_SERVICE_UUID = "7905f431-b5ce-4e99-a40f-4b1e122d00d0"
ANCS_SERVICE_UUID_BYTES = bytes.fromhex("d0002d121e4b0fa4994eceb531f40579")

AD_TYPE_FLAGS = 0x01
AD_TYPE_UUID_16_INCOMPLETE = 0x02
AD_TYPE_UUID_16_COMPLETE = 0x03
AD_TYPE_UUID_32_INCOMPLETE = 0x04
AD_TYPE_UUID_32_COMPLETE = 0x05
AD_TYPE_UUID_128_INCOMPLETE = 0x06
AD_TYPE_UUID_128_COMPLETE = 0x07
AD_TYPE_SHORTENED_LOCAL_NAME = 0x08
AD_TYPE_COMPLETE_LOCAL_NAME = 0x09
AD_TYPE_TX_POWER = 0x0A
AD_TYPE_SERVICE_SOLICITATION_16 = 0x14
AD_TYPE_SERVICE_SOLICITATION_128 = 0x15
AD_TYPE_SERVICE_DATA_16 = 0x16
AD_TYPE_APPEARANCE = 0x19
AD_TYPE_MANUFACTURER_SPECIFIC = 0xFF

# HID over GATT service; advertising it is what makes iOS list the accessory.
HID_SERVICE_UUID_16 = 0x1812
HID_APPEARANCE_KEYBOARD = 0x03C1

# AD types whose payload is a list of 128-bit UUIDs (solicitations first: that
# is the difference between "this accessory wants ANCS" and "this device has a
# service").
UUID128_SOLICITATION_TYPES = (AD_TYPE_SERVICE_SOLICITATION_128,)
UUID128_LIST_TYPES = (AD_TYPE_UUID_128_INCOMPLETE, AD_TYPE_UUID_128_COMPLETE)
UUID16_LIST_TYPES = (AD_TYPE_UUID_16_INCOMPLETE, AD_TYPE_UUID_16_COMPLETE)

LEGACY_ADVERTISING_LIMIT = 31

AD_NAMES = {
    AD_TYPE_FLAGS: "Flags",
    AD_TYPE_UUID_16_INCOMPLETE: "16-bit service UUIDs (incomplete)",
    AD_TYPE_UUID_16_COMPLETE: "16-bit service UUIDs (complete)",
    AD_TYPE_UUID_32_INCOMPLETE: "32-bit service UUIDs (incomplete)",
    AD_TYPE_UUID_32_COMPLETE: "32-bit service UUIDs (complete)",
    AD_TYPE_UUID_128_INCOMPLETE: "128-bit service UUIDs (incomplete)",
    AD_TYPE_UUID_128_COMPLETE: "128-bit service UUIDs (complete)",
    AD_TYPE_SHORTENED_LOCAL_NAME: "Shortened local name",
    AD_TYPE_COMPLETE_LOCAL_NAME: "Complete local name",
    AD_TYPE_TX_POWER: "TX power level",
    AD_TYPE_SERVICE_SOLICITATION_16: "16-bit service solicitation",
    AD_TYPE_SERVICE_SOLICITATION_128: "128-bit service solicitation",
    AD_TYPE_SERVICE_DATA_16: "16-bit service data",
    AD_TYPE_APPEARANCE: "Appearance",
    AD_TYPE_MANUFACTURER_SPECIFIC: "Manufacturer specific data",
}

# Bluetooth Assigned Numbers §2.6, only the values this project can advertise
# plus the neighbours a reviewer is likely to confuse them with.  0x00C0 is
# "Generic Watch" (category 0x03 Watch, subcategory 0x00); 0x0007 is
# "Eye-glasses" and belongs to the "Generic" category, not to Watch.
APPEARANCE_NAMES = {
    0x0000: "Unknown",
    0x0001: "Phone",
    0x0002: "Computer",
    0x0003: "Watch",
    0x0004: "Clock",
    0x0005: "Display",
    0x0006: "Remote Control",
    0x0007: "Eye-glasses",
    0x0008: "Tag",
    0x0009: "Keyring",
    0x000A: "Media Player",
    0x00C0: "Generic Watch",
    0x00C1: "Sports Watch",
    0x00C2: "Smartwatch",
    0x00C3: "Watch (Clock)",
    0x03C0: "Generic HID",
    0x03C1: "Keyboard",
    0x03C2: "Mouse",
    0x03C3: "Joystick",
    0x03C4: "Gamepad",
}


def appearance_name(value: int) -> str:
    """Assigned-number name for an appearance value, e.g. 0x00C0."""
    known = APPEARANCE_NAMES.get(value)
    if known is not None:
        return known
    return f"unknown (category 0x{value >> 6:02X}, subcategory 0x{value & 0x3F:02X})"


def uuid128_from_little_endian(raw: bytes) -> str:
    """Canonical text form of a 16-byte on-air (little-endian) UUID."""
    text = raw[::-1].hex()
    return f"{text[0:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:32]}"


@dataclass(frozen=True)
class AdElement:
    """One length/type/value triple from an advertising report."""

    ad_type: int
    payload: bytes
    offset: int

    @property
    def name(self) -> str:
        return AD_NAMES.get(self.ad_type, f"AD type 0x{self.ad_type:02X}")

    @property
    def is_ancs_solicitation(self) -> bool:
        if self.ad_type not in UUID128_SOLICITATION_TYPES:
            return False
        return ANCS_SERVICE_UUID in self.uuids

    @property
    def uuids(self) -> tuple[str, ...]:
        """128-bit UUIDs carried by this element, in text form."""
        if self.ad_type not in UUID128_SOLICITATION_TYPES + UUID128_LIST_TYPES:
            return ()
        chunks = [
            self.payload[index : index + 16] for index in range(0, len(self.payload) - 15, 16)
        ]
        return tuple(uuid128_from_little_endian(chunk) for chunk in chunks)

    @property
    def uuids16(self) -> tuple[int, ...]:
        """16-bit UUIDs carried by this element (service lists only)."""
        if self.ad_type not in UUID16_LIST_TYPES:
            return ()
        return tuple(
            int.from_bytes(self.payload[index : index + 2], "little")
            for index in range(0, len(self.payload) - 1, 2)
        )

    def describe(self) -> str:
        head = f"0x{self.ad_type:02X} {self.name}"
        if self.ad_type == AD_TYPE_APPEARANCE and len(self.payload) == 2:
            value = int.from_bytes(self.payload, "little")
            return f"{head} 0x{value:04X} ({appearance_name(value)})"
        if self.ad_type == AD_TYPE_FLAGS:
            return f"{head} 0x{self.payload[0]:02X}" if self.payload else head
        if self.ad_type in (
            AD_TYPE_COMPLETE_LOCAL_NAME,
            AD_TYPE_SHORTENED_LOCAL_NAME,
        ):
            return f'{head} "{_decode_name(self.payload)}"'
        if self.ad_type in UUID128_SOLICITATION_TYPES + UUID128_LIST_TYPES:
            return f"{head} {', '.join(self.uuids)}"
        if self.ad_type in UUID16_LIST_TYPES:
            rendered = ", ".join(f"0x{uuid:04X}" for uuid in self.uuids16)
            return f"{head} {rendered}"
        if self.ad_type == AD_TYPE_MANUFACTURER_SPECIFIC and len(self.payload) >= 2:
            company = int.from_bytes(self.payload[:2], "little")
            return f"{head} company 0x{company:04X}"
        return f"{head} {self.payload.hex()}"


@dataclass(frozen=True)
class AdvertisingReport:
    """A parsed advertising packet plus the ANCS-specific verdicts."""

    raw: bytes = b""
    elements: tuple[AdElement, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def size(self) -> int:
        return len(self.raw)

    @property
    def ancs_solicitations(self) -> tuple[AdElement, ...]:
        return tuple(element for element in self.elements if element.is_ancs_solicitation)

    @property
    def has_ancs_solicitation(self) -> bool:
        """True when a 0x15 element solicits the ANCS UUID."""
        return bool(self.ancs_solicitations)

    @property
    def ancs_uuid_lists(self) -> tuple[AdElement, ...]:
        return tuple(
            element
            for element in self.elements
            if element.ad_type in UUID128_LIST_TYPES and ANCS_SERVICE_UUID in element.uuids
        )

    @property
    def appearance(self) -> int | None:
        for element in self.elements:
            if element.ad_type == AD_TYPE_APPEARANCE and len(element.payload) == 2:
                return int.from_bytes(element.payload, "little")
        return None

    @property
    def appearance_name(self) -> str:
        value = self.appearance
        return "" if value is None else appearance_name(value)

    @property
    def has_hid_service(self) -> bool:
        """True when a 0x03 element advertises the HID over GATT service."""
        return any(
            HID_SERVICE_UUID_16 in element.uuids16
            for element in self.elements
            if element.ad_type in UUID16_LIST_TYPES
        )

    @property
    def local_name(self) -> str:
        for element in self.elements:
            if element.ad_type in (
                AD_TYPE_COMPLETE_LOCAL_NAME,
                AD_TYPE_SHORTENED_LOCAL_NAME,
            ):
                return _decode_name(element.payload)
        return ""

    @property
    def manufacturer_ids(self) -> tuple[int, ...]:
        return tuple(
            int.from_bytes(element.payload[:2], "little")
            for element in self.elements
            if element.ad_type == AD_TYPE_MANUFACTURER_SPECIFIC and len(element.payload) >= 2
        )

    def warnings(self, *, expect_ancs: bool = True) -> list[str]:
        """Everything that would stop an iPhone from offering ANCS.

        `expect_ancs` is False for the scan response, which carries the name and
        is not supposed to solicit the service itself.
        """
        found = list(self.problems)
        if expect_ancs and not self.has_ancs_solicitation:
            if self.ancs_uuid_lists:
                found.append(
                    "The ANCS UUID appears only in a 0x06/0x07 UUID list. That "
                    "advertises a service this device offers; iOS needs the 0x15 "
                    "service solicitation to treat it as a notification consumer."
                )
            else:
                found.append(
                    "No 0x15 element, and no ANCS UUID at all: the iPhone will "
                    "not offer the notification service to this accessory."
                )
        if self.size > LEGACY_ADVERTISING_LIMIT:
            found.append(
                f"{self.size} bytes do not fit the {LEGACY_ADVERTISING_LIMIT}-byte "
                "legacy advertising PDU; the controller truncates or rejects it."
            )
        if expect_ancs and self.appearance is None and self.elements:
            found.append("No 0x19 Appearance element: iOS shows a generic icon.")
        return found

    def describe(self, *, expect_ancs: bool = True) -> str:
        """Human-readable dump: every element, then the ANCS verdict."""
        lines = [f"Advertising data ({self.size} bytes):"]
        if not self.elements:
            lines.append("  (empty)")
        for element in self.elements:
            lines.append(f"  +{element.offset:02d}  {element.describe()}")
        for warning in self.warnings(expect_ancs=expect_ancs):
            lines.append(f"  ! {warning}")
        if expect_ancs:
            verdict = (
                "ANCS solicited through 0x15"
                if self.has_ancs_solicitation
                else "ANCS NOT solicited through 0x15"
            )
            lines.append(f"  => {verdict}")
        return "\n".join(lines)


def _decode_name(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace")


def parse_elements(data: bytes) -> tuple[list[AdElement], list[str]]:
    """Split a raw advertising payload into elements, tolerating bad lengths."""
    elements: list[AdElement] = []
    problems: list[str] = []
    offset = 0
    while offset < len(data):
        length = data[offset]
        if length == 0:
            # A zero length byte is padding at the end of a legacy PDU.
            if offset + 1 < len(data):
                problems.append(f"{len(data) - offset} padding bytes after offset {offset}.")
            break
        end = min(offset + 1 + length, len(data))
        if end < offset + 1 + length:
            problems.append(
                f"Element at offset {offset} claims {length} bytes but only "
                f"{len(data) - offset - 1} remain."
            )
        if length >= 1:
            elements.append(
                AdElement(ad_type=data[offset + 1], payload=data[offset + 2 : end], offset=offset)
            )
        offset = end
    return elements, problems


def analyze_advertising(data: bytes, *, problems: tuple[str, ...] = ()) -> AdvertisingReport:
    """Parse *data* and answer the questions the ANCS checklist asks."""
    elements, parse_problems = parse_elements(bytes(data or b""))
    return AdvertisingReport(
        raw=bytes(data or b""),
        elements=tuple(elements),
        problems=tuple(problems) + tuple(parse_problems),
    )


@dataclass(frozen=True)
class AncsAdvertising:
    """Advertising PDU + scan response of the accessory, as bytes."""

    advertising: bytes = b""
    scan_response: bytes = b""
    hid: bool = False
    notes: tuple[str, ...] = field(default=())

    @property
    def report(self) -> AdvertisingReport:
        return analyze_advertising(self.advertising, problems=self.notes)

    @property
    def scan_report(self) -> AdvertisingReport:
        return analyze_advertising(self.scan_response)

    def describe(self) -> str:
        # The scan response is what carries the name; it has its own budget and
        # is not expected to repeat the solicitation.
        lines = [self.report.describe()]
        if self.hid:
            if self.report.has_hid_service:
                lines.append(
                    f"  => HID advertised: 0x{HID_SERVICE_UUID_16:04X} is in the 0x03 "
                    "service list, so iOS lists the accessory in Settings"
                )
            else:
                lines.append(
                    "  => HID NOT advertised: even with ANCS solicited, iOS may "
                    "never show the accessory"
                )
        lines.extend(["", "Scan response:", self.scan_report.describe(expect_ancs=False)])
        return "\n".join(lines)
