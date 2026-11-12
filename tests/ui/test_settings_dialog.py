"""The settings dialog must appear even though the Tk root stays hidden."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.bluetooth import BluetoothDeviceInfo, SinkState
from bark_pc.ui.settings_dialog import SettingsDialog, _device_choices


class FakeController:
    def preview_tts(self, **_kwargs) -> bool:
        return False

    def bluetooth_support(self):
        return False, "Bluetooth is not exercised by this fake."

    def bluetooth_state(self) -> SinkState:
        return SinkState()

    def bluetooth_devices(self):
        return [], ""


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


def test_bluetooth_tab_is_present(hidden_root, settings_store):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        labels = [
            dialog.notebook.tab(tab, "text") for tab in dialog.notebook.tabs()
        ]
        assert "Bluetooth" in labels
    finally:
        dialog.close()


def test_saved_bluetooth_values_are_loaded_and_included(hidden_root, settings_store):
    settings_store.update(
        bluetooth_enabled=True,
        bluetooth_device_id="device-id-1",
        bluetooth_device_name="Pixel 9",
    )
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        assert dialog.bluetooth_var.get() is True
        candidate = dialog._candidate()
        assert candidate["bluetooth_enabled"] is True
        assert candidate["bluetooth_device_id"] == "device-id-1"
        assert candidate["bluetooth_device_name"] == "Pixel 9"
    finally:
        dialog.close()


def test_enabling_bluetooth_without_a_device_is_rejected(
    hidden_root, settings_store, monkeypatch
):
    errors: list[tuple] = []
    monkeypatch.setattr(
        "bark_pc.ui.settings_dialog.messagebox.showerror",
        lambda *args, **kwargs: errors.append(args),
    )
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        dialog.bluetooth_var.set(True)
        dialog._save()
        assert errors
        assert settings_store.settings.bluetooth_enabled is False
    finally:
        dialog.close()


def test_bluetooth_device_labels_are_unique():
    choices = _device_choices(
        [
            BluetoothDeviceInfo("device-a", "Pixel 9"),
            BluetoothDeviceInfo("device-b", "Pixel 9"),
        ]
    )
    assert [label for label, _device in choices] == ["Pixel 9", "Pixel 9 (2)"]
    assert [device.id for _label, device in choices] == ["device-a", "device-b"]


def test_connect_uses_the_selected_bluetooth_device(hidden_root, settings_store):
    class Controller(FakeController):
        def __init__(self):
            self.calls: list[tuple] = []

        def bluetooth_support(self):
            return True, ""

        def bluetooth_devices(self):
            return [BluetoothDeviceInfo("device-a", "Pixel 9")], ""

        def bluetooth_connect(self, device_id, device_name=""):
            self.calls.append((device_id, device_name))

    controller = Controller()
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=controller
    )
    try:
        dialog.bluetooth_var.set(True)
        dialog._refresh_bluetooth_devices(select_id="device-a")
        assert dialog.bluetooth_combo.get() == "Pixel 9"
        dialog._bluetooth_connect()
        assert controller.calls == [("device-a", "Pixel 9")]
    finally:
        dialog.close()
