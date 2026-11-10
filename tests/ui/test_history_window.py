"""Deleting a notification keeps the list usable: the next row stays selected."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.core.models import Notification
from bark_pc.ui.history_window import HistoryWindow


@pytest.fixture
def hidden_root():
    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - no display (headless CI)
        pytest.skip("Tk is not available in this environment")
    root.withdraw()
    try:
        yield root
    finally:
        root.destroy()


def _store(db, count: int) -> list[Notification]:
    """Insert *count* notifications in ascending time order (one per second)."""
    stored = []
    for index in range(count):
        notification = Notification(
            remote_id=f"remote-{index}",
            title=f"title-{index}",
            body=f"body-{index}",
            received_at=1_700_000_000_000 + index * 1000,
            source_server="https://example.test",
        )
        notification.id = db.insert_notification(notification)
        stored.append(notification)
    return stored


@pytest.fixture
def window(hidden_root, db):
    created = HistoryWindow(hidden_root, db=db)
    created.window.withdraw()
    try:
        yield created
    finally:
        created.destroy()


def _select(window, notification_id: int) -> None:
    window.select_notification(notification_id)
    window.window.update()


def _titles(window) -> list[str]:
    return [window._rows[item].title for item in window.tree.get_children()]


def test_delete_middle_selects_the_next_row(hidden_root, db, window):
    oldest, middle, newest = _store(db, 3)
    window.refresh()
    _select(window, middle.id)

    window._delete_selected()

    assert window._selected is not None
    assert window._selected.id == oldest.id
    assert _titles(window) == [newest.title, oldest.title]


def test_delete_bottom_row_selects_the_previous_one(hidden_root, db, window):
    oldest, middle, newest = _store(db, 3)
    window.refresh()
    _select(window, oldest.id)

    window._delete_selected()

    assert window._selected is not None
    assert window._selected.id == middle.id
    assert _titles(window) == [newest.title, middle.title]


def test_delete_top_row_selects_the_next_one(hidden_root, db, window):
    oldest, middle, newest = _store(db, 3)
    window.refresh()
    _select(window, newest.id)

    window._delete_selected()

    assert window._selected is not None
    assert window._selected.id == middle.id
    assert _titles(window) == [middle.title, oldest.title]


def test_delete_only_row_clears_the_detail_pane(hidden_root, db, window):
    (only,) = _store(db, 1)
    window.refresh()
    _select(window, only.id)

    window._delete_selected()

    assert window._selected is None
    assert window.tree.get_children() == ()
    assert window.detail_title.cget("text") == "No notification selected"


def test_delete_last_loaded_row_pulls_in_the_next_page(hidden_root, db, window, monkeypatch):
    monkeypatch.setattr("bark_pc.ui.history_window.PAGE_SIZE", 2)
    oldest, middle, newest = _store(db, 3)
    window.refresh()
    assert _titles(window) == [newest.title, middle.title]
    _select(window, middle.id)

    window._delete_selected()

    assert window._selected is not None
    assert window._selected.id == oldest.id
    assert _titles(window) == [newest.title, oldest.title]
