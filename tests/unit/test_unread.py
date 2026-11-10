"""Unread counts and the 9+ badge."""

from __future__ import annotations

from bark_pc.core.models import Notification, now_ms
from bark_pc.platform.windows.tray import format_badge


def seed(db, count: int) -> list[int]:
    base = now_ms()
    return [
        db.insert_notification(
            Notification(
                remote_id=f"u-{index}",
                title=f"m{index}",
                body="b",
                received_at=base + index,
                source_server="api.day.app",
            )
        )
        for index in range(count)
    ]


def test_unread_excludes_read_rows(db):
    ids = seed(db, 3)
    db.mark_read(ids[1])
    assert db.unread_count() == 2


def test_unread_excludes_soft_deleted_rows(db):
    ids = seed(db, 2)
    db.connection.execute("UPDATE notification SET deleted_at = ? WHERE id = ?", (now_ms(), ids[0]))
    assert db.unread_count() == 1


def test_unread_is_zero_after_clear(db):
    seed(db, 5)
    db.clear_history()
    assert db.unread_count() == 0


def test_marking_read_in_any_order_is_consistent(db):
    ids = seed(db, 4)
    for index in (2, 0, 3, 1):
        db.mark_read(ids[index])
    assert db.unread_count() == 0
    for notification_id in ids:
        assert db.get_notification(notification_id).read_at is not None


def test_badge_caps_at_nine_plus():
    assert format_badge(0) == "0"
    assert format_badge(1) == "1"
    assert format_badge(9) == "9"
    assert format_badge(10) == "9+"
    assert format_badge(999) == "9+"
    assert format_badge(-3) == "0"


def test_unread_count_tracks_inserts_and_deletes(db):
    ids = seed(db, 3)
    assert db.unread_count() == 3
    db.delete_notification(ids[0])
    assert db.unread_count() == 2
    db.mark_read(None)
    assert db.unread_count() == 0
