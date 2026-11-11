"""Deleting a notification keeps the list usable: the next row stays selected."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.core.models import BarkLink, LinkState, Notification
from bark_pc.ui.history_window import HistoryWindow
from bark_pc.ui.theme import SEND_OFFLINE_BG, SEND_ONLINE_BG


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


def _compose_window(root, db, **callbacks):
    created = HistoryWindow(root, db=db, callbacks=callbacks)
    created.window.withdraw()
    return created


def test_send_button_is_disabled_until_realtime_is_connected(hidden_root, db):
    state = {"connected": False}
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: state["connected"],
        on_send_message=lambda _text: True,
    )
    try:
        assert window.send_button.cget("state") == "disabled"
        assert window.send_button.cget("background") == SEND_OFFLINE_BG

        state["connected"] = True
        window.refresh_status()
        assert window.send_button.cget("state") == "normal"
        assert window.send_button.cget("background") == SEND_ONLINE_BG

        state["connected"] = False
        window.refresh_status()
        assert window.send_button.cget("state") == "disabled"
        assert window.send_button.cget("background") == SEND_OFFLINE_BG
    finally:
        window.destroy()


def test_send_button_colour_matches_the_tray_icon_states():
    from bark_pc.core.models import ConnectionStatus
    from bark_pc.platform.windows.tray import STATUS_COLORS

    def rgb(color: str) -> tuple[int, int, int]:
        return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))

    assert rgb(SEND_ONLINE_BG) == STATUS_COLORS[ConnectionStatus.ACTIVE][:3]
    assert rgb(SEND_OFFLINE_BG) == STATUS_COLORS[ConnectionStatus.STARTING][:3]


def test_composer_sends_on_enter_and_clears_the_box(hidden_root, db):
    sent: list[str] = []
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: True,
        on_send_message=lambda text: sent.append(text) or True,
    )
    try:
        window.message_var.set("  hello room  ")
        assert window._on_composer_return() == "break"
        assert sent == ["hello room"]
        assert window.message_var.get() == ""
    finally:
        window.destroy()


def test_composer_ignores_sends_while_disconnected(hidden_root, db):
    sent: list[str] = []
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: False,
        on_send_message=lambda text: sent.append(text) or True,
    )
    try:
        window.message_var.set("dropped")
        window._send_message()
        assert sent == []
        assert window.message_var.get() == "dropped"
    finally:
        window.destroy()


def test_composer_entry_is_as_tall_as_the_send_button(hidden_root, db):
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: True,
        on_send_message=lambda _text: True,
    )
    try:
        window.window.deiconify()
        window.window.update()
        assert window.message_entry.winfo_height() == window.send_button.winfo_height()
    finally:
        window.destroy()


def test_delete_and_send_buttons_share_the_right_edge(hidden_root, db):
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: True,
        on_send_message=lambda _text: True,
    )
    try:
        window.window.deiconify()
        window.window.update()
        right = window.window.winfo_rootx() + window.window.winfo_width()
        delete_gap = right - (
            window.delete_button.winfo_rootx() + window.delete_button.winfo_width()
        )
        send_gap = right - (
            window.send_button.winfo_rootx() + window.send_button.winfo_width()
        )
        assert delete_gap == send_gap
    finally:
        window.destroy()


def test_composer_keeps_the_text_when_the_callback_refuses(hidden_root, db):
    window = _compose_window(
        hidden_root,
        db,
        supabase_connected=lambda: True,
        on_send_message=lambda _text: False,
    )
    try:
        window.message_var.set("retry me")
        window._send_message()
        assert window.message_var.get() == "retry me"
    finally:
        window.destroy()


def _seed_link(db, url: str) -> None:
    db.upsert_link(
        BarkLink(server_url=url, device_key="seedKey123", state=LinkState.LINKED)
    )


def test_toolbar_prefixes_the_status_with_the_server_host(hidden_root, db):
    _seed_link(db, "https://api.day.app")
    window = _compose_window(hidden_root, db, connection_status=lambda: "Connected")
    try:
        assert window.status_label.cget("text") == "api.day.app · Connected"
    finally:
        window.destroy()


def test_toolbar_keeps_the_port_of_a_self_hosted_server(hidden_root, db):
    _seed_link(db, "http://127.0.0.1:8080")
    window = _compose_window(
        hidden_root, db, connection_status=lambda: "Reconnecting…"
    )
    try:
        assert window.status_label.cget("text") == "127.0.0.1:8080 · Reconnecting…"
    finally:
        window.destroy()


def test_toolbar_shows_only_the_state_without_a_link(hidden_root, db):
    window = _compose_window(hidden_root, db)
    try:
        assert window.status_label.cget("text") == "Starting"
    finally:
        window.destroy()


def test_toolbar_appends_a_notice_after_the_host_and_status(hidden_root, db):
    _seed_link(db, "https://api.day.app")
    window = _compose_window(
        hidden_root,
        db,
        connection_status=lambda: "Connected",
        toast_notice=lambda: "toasts blocked",
    )
    try:
        assert window.status_label.cget("text") == "api.day.app · Connected · toasts blocked"
    finally:
        window.destroy()


def test_compose_bar_uses_the_general_route_state(hidden_root, db):
    """A Bark-URL route enables Send even when Realtime is disconnected."""
    sent: list[str] = []
    window = _compose_window(
        hidden_root,
        db,
        can_send_message=lambda: True,
        supabase_connected=lambda: False,
        on_send_message=lambda text: sent.append(text) or True,
    )
    try:
        assert window.send_button.cget("state") == "normal"
        window.message_var.set("to the phone")
        window._send_message()
        assert sent == ["to the phone"]
        assert window.message_var.get() == ""
    finally:
        window.destroy()
