"""Retention: prune by age and by count, newest kept, oldest deleted first."""

from __future__ import annotations

from bark_pc.core.models import Notification, now_ms

DAY_MS = 24 * 60 * 60 * 1000


def seed(db, count: int, *, start: int | None = None) -> list[int]:
    base = start if start is not None else now_ms()
    ids = []
    for index in range(count):
        notification = Notification(
            remote_id=f"m-{index}",
            title=f"message {index}",
            body="body",
            received_at=base + index * 1000,
            source_server="api.day.app",
        )
        ids.append(db.insert_notification(notification))
    return ids


def test_prune_by_age_removes_only_old_rows(db):
    now = now_ms()
    old = db.insert_notification(
        Notification(remote_id="old", body="old", received_at=now - 40 * DAY_MS)
    )
    fresh = db.insert_notification(
        Notification(remote_id="fresh", body="fresh", received_at=now - 1 * DAY_MS)
    )

    counts = db.prune(retention_count=500, retention_days=30, now=now)

    assert counts["by_age"] == 1
    assert counts["by_count"] == 0
    remaining = [row.remote_id for row in db.list_notifications()]
    assert remaining == ["fresh"]
    assert db.get_notification(old) is None
    assert db.get_notification(fresh) is not None


def test_prune_by_count_keeps_the_newest_and_deletes_the_oldest_first(db):
    ids = seed(db, 10)
    counts = db.prune(retention_count=4, retention_days=365)
    assert counts["by_count"] == 6
    remaining = db.list_notifications()
    assert len(remaining) == 4
    assert [row.remote_id for row in remaining] == ["m-9", "m-8", "m-7", "m-6"]
    for stale_id in ids[:6]:
        assert db.get_notification(stale_id) is None


def test_prune_is_idempotent(db):
    seed(db, 5)
    assert db.prune(3, 30)["by_count"] == 2
    assert db.prune(3, 30)["by_count"] == 0
    assert db.count_notifications() == 3


def test_prune_with_both_limits_deletes_the_union(db):
    now = now_ms()
    # Two ancient rows plus six recent rows; keep 3 of the recent ones.
    for index in range(2):
        db.insert_notification(
            Notification(remote_id=f"old-{index}", body="x", received_at=now - 90 * DAY_MS)
        )
    for index in range(6):
        db.insert_notification(
            Notification(remote_id=f"new-{index}", body="x", received_at=now - index * 1000)
        )
    counts = db.prune(retention_count=3, retention_days=30, now=now)
    assert counts["by_age"] == 2
    assert counts["by_count"] == 3
    assert db.count_notifications() == 3


def test_prune_never_touches_unread_state_of_kept_rows(db):
    seed(db, 3)
    db.mark_read(db.list_notifications()[-1].id)
    db.prune(2, 30)
    rows = db.list_notifications()
    assert len(rows) == 2
    assert all(row.read_at is None for row in rows)
