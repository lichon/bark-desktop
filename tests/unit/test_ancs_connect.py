"""The diagnostic CLI's pure parts: the appearance parser and security report.

No dongle, no radio, no Bumble: the tool has to be trustworthy on a machine
where the adapter is busy or absent.
"""

from __future__ import annotations

import argparse

import pytest

from bark_pc.tools import ancs_connect


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0x00C0", 0x00C0), ("192", 192), ("0x0007", 0x0007), ("0", 0), ("0xFFFF", 0xFFFF)],
)
def test_the_appearance_argument_accepts_hex_and_decimal(text, expected):
    assert ancs_connect._appearance_value(text) == expected


@pytest.mark.parametrize("text", ["", "watch", "-1", "0x10000", "70000"])
def test_bad_appearance_values_are_rejected(text):
    with pytest.raises(argparse.ArgumentTypeError):
        ancs_connect._appearance_value(text)


def test_the_appearance_default_follows_the_hid_choice():
    """No explicit value: keyboard with HID (the default), watch armband without."""
    parser = ancs_connect.build_parser()

    assert parser.parse_args([]).appearance is None
    assert parser.parse_args([]).no_hid is False
    assert parser.parse_args(["--appearance", "0x0007"]).appearance == 0x0007
    assert parser.parse_args(["--no-hid"]).no_hid is True


def test_mitm_and_bonding_are_opt_in_flags():
    parser = ancs_connect.build_parser()

    plain = parser.parse_args([])
    assert (plain.mitm, plain.no_bonding) == (False, False)
    assert parser.parse_args(["--mitm", "--no-bonding"]).mitm is True


def test_the_security_block_reports_bonding_mitm_and_hid():
    text = ancs_connect.format_security({"bonding": True, "mitm_requested": True, "hid": True})

    assert "bonding=on" in text
    assert "MITM=on" in text
    assert "HID=keyboard 0x1812" in text
    assert "Link: not connected" in text

    plain = ancs_connect.format_security({"bonding": True, "mitm_requested": False})
    assert "HID=off" in plain


def test_an_authenticated_link_is_called_out():
    text = ancs_connect.format_security(
        {
            "connected": True,
            "bonding": True,
            "mitm_requested": True,
            "encrypted": True,
            "authenticated": True,
            "secure_connections": True,
            "key_size": 16,
            "role": "PERIPHERAL",
            "peer_mitm_requested": True,
        }
    )

    assert "authenticated=yes" in text
    assert "key=16B" in text
    assert "the link is authenticated" in text
    assert "also asked for MITM" in text


def test_a_just_works_fallback_is_not_mistaken_for_mitm():
    text = ancs_connect.format_security(
        {
            "connected": True,
            "bonding": True,
            "mitm_requested": True,
            "encrypted": True,
            "authenticated": False,
            "secure_connections": True,
            "key_size": 16,
            "role": "CENTRAL",
            "peer_mitm_requested": False,
        }
    )

    assert "NOT authenticated" in text
    assert "Just Works" in text


def test_the_advertising_dump_mentions_the_0x15_field():
    pytest.importorskip("bumble")

    text = ancs_connect.dump_advertising(0x0007)

    assert "0x15 128-bit service solicitation" in text
    assert "7905f431-b5ce-4e99-a40f-4b1e122d00d0" in text
    assert "0x0007" in text
    assert "ANCS solicited through 0x15" in text


def test_the_advertising_dump_lists_the_hid_service_by_default():
    pytest.importorskip("bumble")

    with_hid = ancs_connect.dump_advertising()
    assert "0x03 16-bit service UUIDs (complete) 0x1812" in with_hid
    assert "Appearance 0x03C1 (Keyboard)" in with_hid
    assert "HID advertised" in with_hid

    without_hid = ancs_connect.dump_advertising(None, hid=False)
    assert "0x1812" not in without_hid
    assert "Appearance 0x00C0 (Generic Watch)" in without_hid
    assert "HID" not in without_hid
