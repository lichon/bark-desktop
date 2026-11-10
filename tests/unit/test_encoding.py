"""Text encoding and layout hardening for CJK, RTL, emoji and long bodies."""

from __future__ import annotations

import json

from bark_pc.core.models import BODY_MAX_STORED_BYTES, TOAST_MAX_CHARS, Notification, now_ms
from bark_pc.push.payload import ENCRYPTED_PLACEHOLDER, parse_payload
from bark_pc.ui.history_window import format_time

CJK = "中文通知内容测试"
RTL = "هذا إشعار باللغة العربية"
EMOJI = "deploy 🚀 done ✅"
MIXED = f"{CJK} {EMOJI} {RTL}"


def parse(alert: dict) -> Notification:
    raw = json.dumps({"aps": {"alert": alert}}, ensure_ascii=False).encode("utf-8")
    return parse_payload(raw, source_server="api.day.app").notification


def test_cjk_emoji_and_rtl_survive_parsing():
    notification = parse({"title": MIXED, "body": MIXED})
    assert notification.title == MIXED
    assert notification.body == MIXED
    assert notification.display_title == MIXED


def test_storage_roundtrip_preserves_unicode(db):
    notification = parse({"title": MIXED, "subtitle": RTL, "body": EMOJI * 20})
    notification.remote_id = "unicode-1"
    db.insert_notification(notification)

    row = db.list_notifications()[0]
    assert row.title == MIXED
    assert row.subtitle == RTL
    assert row.body == EMOJI * 20
    assert row.display_title == f"{MIXED} — {RTL}"


def test_long_body_is_capped_in_bytes_not_characters(db):
    # 4-byte emoji: 5000 of them are 20 KB, over the 8 KB stored cap.
    notification = parse({"title": "big", "body": "🚀" * 5_000})
    notification.remote_id = "big-emoji"
    db.insert_notification(notification)

    row = db.list_notifications()[0]
    assert len(row.body.encode("utf-8")) <= BODY_MAX_STORED_BYTES
    assert row.body  # not truncated to nothing
    assert "🚀" in row.body


def test_emoji_truncation_never_splits_a_character():
    notification = parse({"title": "🚀" * 300, "body": "✅" * 300})
    title = notification.toast_title
    body = notification.toast_body
    assert len(title) <= TOAST_MAX_CHARS
    assert len(body) <= TOAST_MAX_CHARS
    # No replacement characters introduced by truncation.
    assert "\ufffd" not in title and "\ufffd" not in body


def test_encrypted_placeholder_is_readable():
    raw = json.dumps(
        {"aps": {"alert": {}}, "ciphertext": "AAAA", "id": "e"}, ensure_ascii=False
    ).encode()
    notification = parse_payload(raw).notification
    assert notification.body == ENCRYPTED_PLACEHOLDER
    assert notification.body.startswith("Encrypted message")
    assert "not supported" in notification.body


def test_time_formatting_is_stable_for_local_timestamps():
    assert format_time(None) == ""
    assert format_time(1_762_732_800_000).count(":") == 1
    assert format_time(1_762_732_800_000)[:4] == "2025"


def test_notification_defaults_are_safe():
    notification = Notification(received_at=now_ms())
    assert notification.toast_title == "Bark"  # never an empty toast title
    assert notification.toast_body == ""
    assert notification.display_title == ""
    assert notification.effective_image_url is None
