"""Repository behaviour: ordering, paging, read state, persistence."""

from __future__ import annotations

from bark_pc.core.db import Database
from bark_pc.core.models import Notification, now_ms


def make(remote_id: str, received_at: int, **kwargs) -> Notification:
    return Notification(
        remote_id=remote_id,
        title=kwargs.pop("title", remote_id),
        body=kwargs.pop("body", "body"),
        received_at=received_at,
        source_server=kwargs.pop("source_server", "api.day.app"),
        **kwargs,
    )


def test_ordering_is_newest_first(db):
    base = now_ms() - 10_000
    for index in range(3):
        db.insert_notification(make(f"m-{index}", base + index * 1000))
    rows = db.list_notifications()
    assert [row.remote_id for row in rows] == ["m-2", "m-1", "m-0"]


def test_ties_break_by_insertion_order(db):
    same_time = now_ms()
    db.insert_notification(make("first", same_time))
    db.insert_notification(make("second", same_time))
    assert [row.remote_id for row in db.list_notifications()] == ["second", "first"]


def test_paging_with_limit_and_offset(db):
    base = now_ms() - 200_000
    for index in range(105):
        db.insert_notification(make(f"m-{index:03d}", base + index * 1000))
    first_page = db.list_notifications(limit=100, offset=0)
    second_page = db.list_notifications(limit=100, offset=100)
    assert len(first_page) == 100
    assert len(second_page) == 5
    assert first_page[0].remote_id == "m-104"
    assert second_page[-1].remote_id == "m-000"
    assert set(row.id for row in first_page).isdisjoint(row.id for row in second_page)


def test_mark_read_one_and_all(db):
    base = now_ms()
    first = db.insert_notification(make("a", base))
    second = db.insert_notification(make("b", base + 1))
    third = db.insert_notification(make("c", base + 2))

    assert db.unread_count() == 3
    assert db.mark_read(first) == 1
    assert db.unread_count() == 2
    assert db.mark_read(first) == 0  # already read
    assert db.unread_count() == 2
    assert db.mark_read(None) == 2
    assert db.unread_count() == 0
    assert all(row.read_at is not None for row in db.list_notifications())
    assert {row.id for row in db.list_notifications()} == {first, second, third}


def test_mark_unread(db):
    notification_id = db.insert_notification(make("a", now_ms()))
    db.mark_read(notification_id)
    assert db.unread_count() == 0
    assert db.mark_unread(notification_id) == 1
    assert db.unread_count() == 1


def test_delete_and_clear(db):
    base = now_ms()
    first = db.insert_notification(make("a", base))
    db.insert_notification(make("b", base + 1))
    assert db.delete_notification(first) is True
    assert db.delete_notification(first) is False
    assert db.count_notifications() == 1
    assert db.clear_history() == 1
    assert db.count_notifications() == 0


def test_delete_by_remote_id(db):
    db.insert_notification(make("recall-me", now_ms()))
    assert db.delete_by_remote_id("api.day.app", "recall-me") is True
    assert db.delete_by_remote_id("api.day.app", "recall-me") is False
    assert db.count_notifications() == 0


def test_find_by_remote_id_is_server_scoped(db):
    db.insert_notification(make("same-id", now_ms(), source_server="a.example"))
    db.insert_notification(make("same-id", now_ms(), source_server="b.example"))
    assert db.find_by_remote_id("a.example", "same-id").source_server == "a.example"
    assert db.find_by_remote_id("b.example", "same-id").source_server == "b.example"
    assert db.find_by_remote_id("c.example", "same-id") is None


def test_persistence_across_close_and_reopen(portable_paths):
    first = Database(portable_paths.db_path)
    try:
        notification_id = first.insert_notification(make("persisted", now_ms()))
        first.mark_read(notification_id)
    finally:
        first.close()

    reopened = Database(portable_paths.db_path)
    try:
        rows = reopened.list_notifications()
        assert len(rows) == 1
        assert rows[0].remote_id == "persisted"
        assert rows[0].read_at is not None
        assert reopened.unread_count() == 0
    finally:
        reopened.close()


def test_body_and_raw_json_are_capped(db):
    notification = Notification(
        remote_id="big",
        title="big",
        body="x" * 20_000,
        raw_json='{"a":"' + ("y" * 20_000) + '"}',
        received_at=now_ms(),
        source_server="api.day.app",
    )
    db.insert_notification(notification)
    row = db.list_notifications()[0]
    assert len(row.body.encode("utf-8")) <= 8 * 1024
    assert len(row.raw_json.encode("utf-8")) <= 4 * 1024


def test_schema_migration_is_idempotent(portable_paths):
    first = Database(portable_paths.db_path)
    assert first.schema_version() == 1
    first.close()
    second = Database(portable_paths.db_path)
    try:
        assert second.schema_version() == 1
        second.insert_notification(make("x", now_ms()))
        assert second.count_notifications() == 1
    finally:
        second.close()


def test_deleted_rows_hidden_from_default_listing(db):
    notification_id = db.insert_notification(make("a", now_ms()))
    db.connection.execute(
        "UPDATE notification SET deleted_at = ? WHERE id = ?", (now_ms(), notification_id)
    )
    assert db.list_notifications() == []
    assert len(db.list_notifications(include_deleted=True)) == 1
    assert db.unread_count() == 0
