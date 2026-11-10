"""Instance command protocol and stale-record recovery (no real mutex/socket)."""

from __future__ import annotations

import json

import pytest

from bark_pc.platform.windows.single_instance import (
    InstanceRecord,
    InvalidToken,
    build_message,
    clear_record,
    is_stale_record,
    mutex_name,
    parse_message,
    read_record,
    write_record,
)


def record(port: int = 51823) -> InstanceRecord:
    return InstanceRecord(pid=1234, port=port, token="a" * 64, started_at=1_762_732_800_000)


def test_focus_message_roundtrip():
    line = build_message("token123", "focus")
    assert line.endswith("\n")
    payload = parse_message(line, "token123")
    assert payload == {"token": "token123", "command": "focus"}


def test_open_message_carries_the_notification_id():
    payload = parse_message(build_message("t", "open", notification_id=42), "t")
    assert payload["command"] == "open"
    assert payload["notification_id"] == 42


def test_setup_and_quit_commands_are_supported():
    assert parse_message(build_message("t", "setup"), "t")["command"] == "setup"
    assert parse_message(build_message("t", "quit"), "t")["command"] == "quit"


def test_unknown_command_is_refused():
    with pytest.raises(ValueError):
        build_message("t", "explode")
    with pytest.raises(ValueError):
        parse_message(json.dumps({"token": "t", "command": "explode"}), "t")


def test_invalid_token_is_refused():
    with pytest.raises(InvalidToken):
        parse_message(build_message("wrong", "focus"), "expected")
    with pytest.raises(InvalidToken):
        parse_message(json.dumps({"command": "focus"}), "expected")


def test_malformed_json_is_rejected_without_raising_anything_else():
    for bad in ("", "   ", "{not json", "[]", '"text"', "null"):
        with pytest.raises(ValueError):
            parse_message(bad, "t")


def test_invalid_utf8_is_rejected():
    with pytest.raises(ValueError):
        parse_message(b"\xff\xfe\x00", "t")


def test_notification_id_must_be_numeric():
    with pytest.raises(ValueError):
        parse_message(json.dumps({"token": "t", "command": "open", "notification_id": "x"}), "t")


def test_stale_record_detection_uses_the_injected_checker():
    instance = record()
    assert is_stale_record(instance, lambda host, port: True) is False
    assert is_stale_record(instance, lambda host, port: False) is True
    assert is_stale_record(None, lambda host, port: False) is False


def test_record_roundtrip_and_clear(tmp_path):
    path = tmp_path / "instance.json"
    instance = record()
    write_record(path, instance)

    loaded = read_record(path)
    assert loaded == instance
    assert json.loads(path.read_text(encoding="utf-8"))["port"] == 51823

    clear_record(path)
    assert read_record(path) is None
    clear_record(path)  # idempotent


def test_unreadable_record_is_ignored(tmp_path):
    path = tmp_path / "instance.json"
    path.write_text("{broken", encoding="utf-8")
    assert read_record(path) is None


def test_mutex_name_is_stable_and_per_user():
    first = mutex_name("s-1-5-21-1111")
    second = mutex_name("S-1-5-21-1111")
    other = mutex_name("s-1-5-21-2222")
    assert first == second
    assert first != other
    assert first.startswith("Local\\BarkPcClient.")
    assert len(first.rsplit(".", 1)[1]) == 16
