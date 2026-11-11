"""The routing dialog lists, edits and saves webhook rules."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.push.routing import RouteStore, WebhookRoute
from bark_pc.ui.routing_dialog import RoutingDialog, RuleEditor


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


def test_dialog_shows_existing_rules_and_saves_new_ones(
    hidden_root, portable_paths
):
    store = RouteStore(portable_paths)
    saved: list[list[WebhookRoute]] = []
    dialog = RoutingDialog(
        hidden_root, route_store=store, on_saved=saved.append
    )
    try:
        dialog.routes.append(
            WebhookRoute.from_dict(
                {
                    "name": "CI",
                    "url": "https://example.com/hook",
                    "message_group": "ops",
                }
            )
        )
        dialog._refresh()
        dialog.show()
        hidden_root.update()
        assert dialog.window.winfo_viewable() == 1
        assert dialog.tree.get_children() == ("0",)

        dialog._save()
        hidden_root.update()
    finally:
        dialog.close()

    assert [route.name for route in store.routes] == ["CI"]
    assert [route.name for route in saved[0]] == ["CI"]


def test_rule_editor_validates_and_returns_a_route(hidden_root):
    editor = RuleEditor(hidden_root)
    try:
        assert editor.body_format_var.get() == "JSON"
        editor.name_var.set("Deploy")
        editor.group_var.set("ops")
        editor.title_var.set("prod")
        editor.body_format_var.set("Form (x-www-form-urlencoded)")
        editor._update_body_hint()
        editor.body_text.delete("1.0", "end")
        editor.body_text.insert("1.0", "text={display_title}&descr={body}")
        editor.url_var.set("example.com/hook")
        editor.method_var.set("POST")
        editor.headers_text.delete("1.0", "end")
        editor.headers_text.insert("1.0", "Authorization: Bearer token")
        editor._save()
    finally:
        editor.close()

    assert editor.result is not None
    assert editor.result.name == "Deploy"
    assert editor.result.message_group == "ops"
    assert editor.result.url == "https://example.com/hook"
    assert editor.result.body_format == "form"
    assert editor.result.body == "text={display_title}&descr={body}"
    assert editor.result.headers == {"Authorization": "Bearer token"}
