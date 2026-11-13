"""The HID over GATT keyboard: what iOS sees, and what it can be made to type.

The service only has to be *valid* for iOS to list the accessory; the key
mapping is there for the case where iOS asks the keyboard to type a pairing
code.
"""

from __future__ import annotations

import asyncio

import pytest

from bark_pc.bluetooth import hid


def test_the_report_map_is_a_complete_keyboard_collection():
    report_map = hid.HID_REPORT_MAP

    assert report_map[0] == 0x05 and report_map[1] == 0x01  # Usage Page (Generic Desktop)
    assert report_map[2:4] == bytes([0x09, 0x06])  # Usage (Keyboard)
    assert report_map[4:6] == bytes([0xA1, 0x01])  # Collection (Application)
    assert report_map[-1] == 0xC0  # End Collection
    assert bytes([0x85, hid.REPORT_ID_KEYBOARD]) in report_map  # Report ID 1
    assert len(report_map) > 40


def test_hid_information_is_four_octets_and_normally_connectable():
    information = hid.HID_INFORMATION

    assert len(information) == 4
    assert int.from_bytes(information[0:2], "little") == 0x0111  # bcdHID 1.11
    assert information[2] == 0x00  # country code
    assert information[3] & 0x02  # normally connectable


@pytest.mark.parametrize(
    ("character", "expected"),
    [
        ("a", (0x00, 0x04)),
        ("z", (0x00, 0x1D)),
        ("A", (hid.MODIFIER_LEFT_SHIFT, 0x04)),
        ("1", (0x00, 0x1E)),
        ("0", (0x00, 0x27)),
        ("!", (hid.MODIFIER_LEFT_SHIFT, 0x1E)),
        (" ", (0x00, 0x2C)),
        ("\n", (0x00, 0x28)),
        ("?", (hid.MODIFIER_LEFT_SHIFT, 0x38)),
        ("@", (hid.MODIFIER_LEFT_SHIFT, 0x1F)),
    ],
)
def test_characters_map_to_boot_keyboard_usage_ids(character, expected):
    assert hid.character_to_key(character) == expected


def test_unsupported_characters_are_skipped_not_guessed():
    assert hid.character_to_key("ä") is None
    assert hid.input_report("\x00") is None
    assert hid.typed_reports("aäb") == [
        bytes([0x00, 0x00, 0x04, 0, 0, 0, 0, 0]),
        bytes(8),
        bytes([0x00, 0x00, 0x05, 0, 0, 0, 0, 0]),
        bytes(8),
    ]


def test_a_report_is_a_key_press_and_a_release():
    reports = hid.typed_reports("Hi")

    assert len(reports) == 4
    assert reports[0] == bytes([hid.MODIFIER_LEFT_SHIFT, 0x00, 0x0B, 0, 0, 0, 0, 0])
    assert reports[1] == bytes(8)
    assert reports[2] == bytes([0x00, 0x00, 0x0C, 0, 0, 0, 0, 0])


# -- the GATT service ------------------------------------------------------ #
def make_keyboard(**options):
    pytest.importorskip("bumble")
    return hid.HidKeyboard(**options)


def test_the_service_is_hid_over_gatt_with_the_required_characteristics():
    bumble_gatt = pytest.importorskip("bumble.gatt")
    keyboard = make_keyboard()

    assert keyboard.uuid == bumble_gatt.GATT_HUMAN_INTERFACE_DEVICE_SERVICE
    uuids = {characteristic.uuid for characteristic in keyboard.characteristics}
    expected = {
        bumble_gatt.UUID.from_16_bits(code)
        for code in (
            0x2A4A,  # HID Information
            0x2A4B,  # Report Map
            0x2A4C,  # HID Control Point
            0x2A4E,  # Protocol Mode
            0x2A4D,  # Report (input and output)
            0x2A22,  # Boot Keyboard Input Report
            0x2A32,  # Boot Keyboard Output Report
        )
    }
    assert uuids == expected
    assert len(keyboard.characteristics) == 8


def test_the_report_characteristics_declare_their_report_references():
    bumble_gatt = pytest.importorskip("bumble.gatt")
    keyboard = make_keyboard()

    input_reference = keyboard.input_report.get_descriptor(
        bumble_gatt.GATT_REPORT_REFERENCE_DESCRIPTOR
    )
    output_reference = keyboard.output_report.get_descriptor(
        bumble_gatt.GATT_REPORT_REFERENCE_DESCRIPTOR
    )
    assert input_reference.value == bytes([hid.REPORT_ID_KEYBOARD, hid.REPORT_TYPE_INPUT])
    assert output_reference.value == bytes([hid.REPORT_ID_LEDS, hid.REPORT_TYPE_OUTPUT])


def test_the_gatt_server_accepts_the_service_and_adds_cccds():
    pytest.importorskip("bumble")
    from bumble import gatt_server

    server = gatt_server.Server(None)
    keyboard = make_keyboard()

    server.add_service(keyboard)

    # 1 service + 8 declarations + 8 values + 2 report references + 2 CCCDs.
    assert len(server.attributes) == 21


def test_protocol_mode_and_led_state_are_writable():
    keyboard = make_keyboard()

    assert keyboard.protocol_mode_is_report
    keyboard.protocol_mode.value.write(None, b"\x00")
    assert not keyboard.protocol_mode_is_report
    keyboard.protocol_mode.value.write(None, b"\x07")  # invalid: ignored
    assert not keyboard.protocol_mode_is_report

    seen = []
    keyboard.on_output_report = seen.append
    keyboard.output_report.value.write(None, b"\x02")
    assert keyboard.leds == 0x02
    assert seen == [0x02]


class NotifyingDevice:
    """Records `notify_subscribers` calls like `bumble.device.Device`."""

    def __init__(self):
        self.calls: list[tuple[str, bytes]] = []

    async def notify_subscribers(self, characteristic, value=None) -> None:
        self.calls.append(
            (
                str(characteristic.uuid),
                bytes(characteristic.value if value is None else value),
            )
        )


def test_sending_a_report_updates_and_notifies_both_keyboard_paths():
    keyboard = make_keyboard()
    device = NotifyingDevice()

    asyncio.run(keyboard.send_report(device, bytes([0, 0, 0x04, 0, 0, 0, 0, 0])))

    report = bytes([0, 0, 0x04, 0, 0, 0, 0, 0])
    assert keyboard.input_report.value == report
    assert keyboard.boot_keyboard_input.value == report
    assert [uuid for uuid, _value in device.calls] == [
        "UUID-16:2A4D (Report)",
        "UUID-16:2A22 (Boot Keyboard Input Report)",
    ]


def test_type_text_sends_press_and_release_for_each_key():
    keyboard = make_keyboard()
    device = NotifyingDevice()

    sent = asyncio.run(keyboard.type_text(device, "hi", delay=0))

    assert sent == 4
    # Each report goes to the Report characteristic and the boot keyboard one.
    presses = [
        bytes([0, 0, 0x0B, 0, 0, 0, 0, 0]),
        bytes(8),
        bytes([0, 0, 0x0C, 0, 0, 0, 0, 0]),
        bytes(8),
    ]
    assert [value for _uuid, value in device.calls] == [
        value for report in presses for value in (report, report)
    ]
