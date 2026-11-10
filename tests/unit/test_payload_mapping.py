"""Payload mapping rules from `contracts/push-payload.md`."""

from __future__ import annotations

import json

import pytest

from bark_pc.core.models import TOAST_MAX_CHARS
from bark_pc.push.payload import (
    ENCRYPTED_PLACEHOLDER,
    ParseOutcome,
    extract_markdown_image,
    normalize_custom_fields,
    parse_payload,
)

from ..conftest import load_payload

SERVER = "api.day.app"


def parse(name: str, **kwargs):
    return parse_payload(load_payload(name), source_server=SERVER, **kwargs)


def test_text_payload_maps_title_body_group_and_id():
    parsed = parse("text.json")
    assert parsed.outcome == ParseOutcome.MESSAGE
    notification = parsed.notification
    assert notification is not None
    assert notification.title == "Deploy done"
    assert notification.body == "build 412 is live"
    assert notification.group_name == "ops"
    assert notification.remote_id == "text-1"
    assert notification.source_server == SERVER
    assert parsed.is_message


def test_subtitle_is_merged_into_the_display_title():
    parsed = parse("text_with_subtitle.json")
    notification = parsed.notification
    assert notification.subtitle == "prod"
    assert notification.display_title == "Deploy — prod"
    assert notification.title == "Deploy"


def test_custom_group_wins_over_thread_id():
    parsed = parse("group_custom.json")
    assert parsed.notification.group_name == "custom-value"


def test_thread_id_is_the_group_fallback():
    parsed = parse("group.json")
    assert parsed.notification.group_name == "ops"


def test_delete_push_is_recognized():
    parsed = parse("delete.json")
    assert parsed.outcome == ParseOutcome.DELETE
    assert parsed.remote_id == "c0ffee-uuid"
    assert parsed.notification is None


def test_numeric_delete_flag_is_tolerated():
    parsed = parse("delete_numeric.json")
    assert parsed.outcome == ParseOutcome.DELETE
    assert parsed.remote_id == "num-delete"


def test_delete_without_id_is_ignored_not_fatal():
    parsed = parse("delete_without_id.json")
    assert parsed.outcome == ParseOutcome.EMPTY
    assert "delete" in (parsed.reason or "")


def test_encrypted_push_gets_a_placeholder_body():
    parsed = parse("encrypted.json")
    assert parsed.outcome == ParseOutcome.ENCRYPTED
    assert parsed.notification is not None
    assert parsed.notification.body == ENCRYPTED_PLACEHOLDER
    assert "ciphertext" in (parsed.notification.raw_json or "")


def test_empty_alert_is_ignored():
    parsed = parse("empty_alert.json")
    assert parsed.outcome == ParseOutcome.EMPTY
    assert parsed.notification is None


def test_bare_string_alert_becomes_the_body():
    parsed = parse("string_alert.json")
    assert parsed.outcome == ParseOutcome.MESSAGE
    assert parsed.notification.body == "bare string body"
    assert parsed.notification.title == ""


def test_unknown_fields_are_preserved_but_not_displayed():
    parsed = parse("unknown_fields.json")
    assert parsed.outcome == ParseOutcome.MESSAGE
    raw = json.loads(parsed.notification.raw_json)
    assert raw["weird"] == "x"
    assert raw["nested"] == {"a": 1}
    assert parsed.notification.title == "Unknown fields"


def test_invalid_utf8_does_not_raise():
    parsed = parse("invalid_utf8.bin")
    assert parsed.outcome in (ParseOutcome.MESSAGE, ParseOutcome.MALFORMED)
    # The replacement-character decode should still yield a usable message.
    assert parsed.notification is None or "bad" in parsed.notification.title


def test_duplicate_payload_parses_with_the_same_remote_id():
    first = parse("text.json")
    second = parse("duplicate_id.json")
    assert first.remote_id == second.remote_id == "text-1"
    assert second.notification.body == "second delivery"


def test_image_icon_sets_image_url():
    parsed = parse("image_icon.json")
    assert parsed.notification.image_url == "https://example.com/chart.png"


def test_non_http_icon_is_ignored():
    parsed = parse("icon_non_http.json")
    assert parsed.notification.image_url is None


def test_markdown_image_is_extracted_when_no_icon():
    parsed = parse("markdown_image.json")
    assert parsed.notification.image_url == "https://example.com/m.png"
    # The text itself stays verbatim.
    assert "![chart]" in parsed.notification.body


def test_markdown_extraction_helper_ignores_non_http_urls():
    assert extract_markdown_image("![a](ftp://x/y.png)") is None
    assert extract_markdown_image("no image here") is None


def test_missing_aps_is_not_fatal():
    parsed = parse("no_aps.json")
    assert parsed.outcome == ParseOutcome.EMPTY


def test_non_object_payload_is_malformed():
    parsed = parse_payload(b"[1, 2, 3]")
    assert parsed.outcome == ParseOutcome.MALFORMED
    assert parsed.notification is None


def test_invalid_json_is_malformed_without_raising():
    parsed = parse_payload(b"{not json")
    assert parsed.outcome == ParseOutcome.MALFORMED


def test_custom_fields_are_lowercased_and_stringified():
    custom = normalize_custom_fields({"autoCopy": True, "TTL": 60, "nested": {"a": 1}})
    assert custom["autocopy"] == "true"
    assert custom["ttl"] == "60"
    assert json.loads(custom["nested"]) == {"a": 1}


def test_toast_text_is_truncated_at_200_characters_per_line():
    body = ("x" * 300) + "\n" + ("y" * 50)
    parsed = parse_payload(
        json.dumps({"aps": {"alert": {"title": "t" * 250, "body": body}}}).encode()
    )
    notification = parsed.notification
    assert len(notification.toast_title) == TOAST_MAX_CHARS
    assert notification.toast_title.endswith("…")
    lines = notification.toast_body.splitlines()
    assert len(lines[0]) == TOAST_MAX_CHARS
    assert lines[1] == "y" * 50


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"aps": null}',
        b'{"aps": {"alert": []}}',
        b'{"aps": {"alert": {"title": 123, "body": true}}}',
        b'{"aps": {"alert": {"title": "x"}}, "delete": "0"}',
    ],
)
def test_parsing_never_raises(raw: bytes):
    parsed = parse_payload(raw)
    assert parsed.outcome in tuple(ParseOutcome)


def test_self_marker_marks_the_notification_as_sent_by_this_desktop():
    parsed = parse_payload(
        json.dumps(
            {
                "aps": {"alert": {"body": "hello"}},
                "id": "own-1",
                "bark_desktop_self": "1",
            }
        ).encode()
    )
    assert parsed.notification is not None
    assert parsed.notification.from_self is True


def test_payloads_without_the_self_marker_are_ordinary_notifications():
    assert parse("text.json").notification.from_self is False
