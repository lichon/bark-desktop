"""The settings dialog must appear even though the Tk root stays hidden."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.ui.settings_dialog import SettingsDialog


class FakeController:
    def preview_tts(self, **_kwargs) -> bool:
        return False


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


def test_dialog_is_visible_with_a_withdrawn_root(hidden_root, settings_store):
    dialog = SettingsDialog(hidden_root, settings_store=settings_store, controller=FakeController())
    try:
        dialog.show()
        hidden_root.update()
        # A Toplevel marked transient on a withdrawn root stays withdrawn on
        # Windows, which made the settings window silently never appear.
        assert dialog.window.winfo_viewable() == 1
        assert dialog.window.state() == "normal"
    finally:
        dialog.close()


def test_close_notifies_the_controller(hidden_root, settings_store):
    closed: list[bool] = []
    dialog = SettingsDialog(
        hidden_root,
        settings_store=settings_store,
        controller=FakeController(),
        on_closed=lambda: closed.append(True),
    )
    dialog.show()
    hidden_root.update()
    dialog.close()
    assert closed == [True]


def test_log_level_is_loaded_and_included_in_the_candidate(hidden_root, settings_store):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        assert dialog.log_level_var.get() == "info"
        dialog.log_level_var.set("debug")
        assert dialog._candidate()["log_level"] == "debug"
    finally:
        dialog.close()
