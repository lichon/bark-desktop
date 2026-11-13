"""What the accessory actually broadcasts, decoded without a dongle.

The checklist these tests encode: the ANCS relationship must be advertised as a
0x15 *service solicitation*, not as a 0x06/0x07 service-UUID list, and the
appearance value is cosmetic (it changes the iOS icon only).
"""

from __future__ import annotations

import pytest

from bark_pc.bluetooth import advertising

# Flags + 0x15 ANCS solicitation + appearance, as produced by the backend.
ANCS_PDU = bytes.fromhex(
    "020106"  # Flags: LE general discoverable, BR/EDR not supported
    "1115d0002d121e4b0fa4994eceb531f40579"  # 0x15 + ANCS UUID, little-endian
    "0319c000"  # Appearance 0x00C0
)


def test_the_ancs_solicitation_is_found_in_the_0x15_element():
    report = advertising.analyze_advertising(ANCS_PDU)

    assert report.has_ancs_solicitation
    assert [element.ad_type for element in report.ancs_solicitations] == [0x15]
    assert report.ancs_solicitations[0].uuids == (advertising.ANCS_SERVICE_UUID,)
    assert report.warnings() == []


def test_the_appearance_is_read_little_endian():
    report = advertising.analyze_advertising(ANCS_PDU)

    assert report.appearance == 0x00C0
    assert report.appearance_name == "Generic Watch"


def test_an_appearance_swap_only_changes_the_icon():
    swapped = ANCS_PDU[:-2] + b"\x07\x00"

    report = advertising.analyze_advertising(swapped)

    assert report.appearance == 0x0007
    assert report.appearance_name == "Eye-glasses"
    # 0x0007 is not Generic Watch; the SIG assigns that to 0x00C0.
    assert advertising.appearance_name(0x00C0) == "Generic Watch"
    assert report.has_ancs_solicitation


def test_a_uuid_list_alone_is_not_a_solicitation():
    uuid_list_only = (
        bytes.fromhex("020106") + bytes([0x11, 0x07]) + advertising.ANCS_SERVICE_UUID_BYTES
    )

    report = advertising.analyze_advertising(uuid_list_only)

    assert not report.has_ancs_solicitation
    assert report.ancs_uuid_lists
    assert any("0x15" in warning for warning in report.warnings())


def test_an_empty_packet_is_reported_as_unsolicited():
    report = advertising.analyze_advertising(b"")

    assert report.elements == ()
    assert not report.has_ancs_solicitation
    assert any("No 0x15" in warning for warning in report.warnings())


def test_a_packet_over_the_legacy_limit_is_flagged():
    padding = bytes([3, 0xFF, 0x4C, 0x00]) * 6

    report = advertising.analyze_advertising(ANCS_PDU + padding)

    assert report.size > advertising.LEGACY_ADVERTISING_LIMIT
    assert any(
        "31-byte" in warning or "legacy advertising" in warning for warning in report.warnings()
    )


def test_truncated_elements_are_reported_not_raised():
    report = advertising.analyze_advertising(bytes([0x05, 0x15, 0x01, 0x02]))

    assert report.problems
    assert "claims" in report.problems[0]


def test_the_name_is_decoded_from_the_scan_response():
    report = advertising.analyze_advertising(bytes.fromhex("0d094261726b204465736b746f70"))

    assert report.local_name == "Bark Desktop"
    assert report.describe(expect_ancs=False).count("Bark Desktop") == 1


def test_a_second_ancs_uuid_in_the_same_element_is_still_solicited():
    payload = advertising.ANCS_SERVICE_UUID_BYTES + bytes.fromhex(
        "00000000000000000000000000000000"
    )
    data = bytes([0x02, 0x01, 0x06, 1 + len(payload), 0x15]) + payload

    report = advertising.analyze_advertising(data)

    assert report.has_ancs_solicitation
    assert len(report.ancs_solicitations[0].uuids) == 2


def test_the_backend_advertises_exactly_this_pattern():
    pytest.importorskip("bumble")
    from bark_pc.bluetooth.ancs_backend import ancs_advertising_data

    built = ancs_advertising_data(0x00C0)

    assert built.advertising == ANCS_PDU
    assert built.scan_response == bytes.fromhex("0d094261726b204465736b746f70")
    assert built.report.has_ancs_solicitation
    assert built.report.warnings() == []


def test_the_appearance_is_configurable_and_range_checked():
    pytest.importorskip("bumble")
    from bark_pc.bluetooth.ancs import AncsError
    from bark_pc.bluetooth.ancs_backend import ancs_advertising_data

    report = ancs_advertising_data(0x0007).report
    assert report.appearance == 0x0007

    with pytest.raises(AncsError):
        ancs_advertising_data(0x10000)
