"""The settings dialog must appear even though the Tk root stays hidden."""

from __future__ import annotations

import tkinter as tk

import pytest

from bark_pc.bluetooth import (
    AncsState,
    BluetoothDeviceInfo,
    SinkState,
    UsbBluetoothAdapter,
)
from bark_pc.ui.settings_dialog import (
    STATUS_MAX_CHARS,
    SettingsDialog,
    _adapter_choices,
    _device_choices,
    _one_line_status,
)


class FakeController:
    def preview_tts(self, **_kwargs) -> bool:
        return False

    def bluetooth_support(self):
        return False, "Bluetooth is not exercised by this fake."

    def bluetooth_state(self) -> SinkState:
        return SinkState()

    def bluetooth_devices(self):
        return [], ""

    def ancs_support(self):
        return False, "ANCS is not exercised by this fake."

    def ancs_state(self) -> AncsState:
        return AncsState()

    def ancs_adapters(self):
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
        # iPhone notifications live in the same tab as the A2DP sink.
        assert "iPhone (ANCS)" not in labels
        assert dialog.ancs_check.winfo_exists()
        assert dialog.ancs_combo.winfo_exists()
        assert dialog.ancs_check.winfo_parent() == dialog.bluetooth_check.winfo_parent()
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


def test_ancs_adapter_labels_are_unique():
    choices = _adapter_choices(
        [
            UsbBluetoothAdapter("usb:0A12:0001", "CSR8510 A10", 0x0A12, 0x0001),
            UsbBluetoothAdapter("usb:0A12:0001#1", "CSR8510 A10", 0x0A12, 0x0001),
        ]
    )
    assert [label for label, _adapter in choices] == [
        "CSR8510 A10 (0A12:0001)",
        "CSR8510 A10 (0A12:0001) (2)",
    ]


def test_saved_ancs_values_are_loaded_and_included(hidden_root, settings_store):
    settings_store.update(
        ancs_enabled=True,
        ancs_adapter="usb:0A12:0001",
        ancs_adapter_name="CSR8510 A10",
    )
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        assert dialog.ancs_var.get() is True
        candidate = dialog._candidate()
        assert candidate["ancs_enabled"] is True
        assert candidate["ancs_adapter"] == "usb:0A12:0001"
        assert candidate["ancs_adapter_name"] == "CSR8510 A10"
    finally:
        dialog.close()


def test_selecting_a_usb_adapter_is_included_in_the_candidate(
    hidden_root, settings_store
):
    class Controller(FakeController):
        def ancs_support(self):
            return True, ""

        def ancs_adapters(self):
            return (
                [UsbBluetoothAdapter("usb:0A12:0001", "CSR8510 A10", 0x0A12, 0x0001)],
                "",
            )

    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=Controller()
    )
    try:
        dialog._refresh_ancs_adapters()
        dialog.ancs_var.set(True)
        assert dialog.ancs_combo.get() == "CSR8510 A10 (0A12:0001)"
        candidate = dialog._candidate()
        assert candidate["ancs_adapter"] == "usb:0A12:0001"
        assert candidate["ancs_adapter_name"] == "CSR8510 A10"
    finally:
        dialog.close()


def test_enabling_ancs_without_an_adapter_is_rejected(
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
        dialog.ancs_var.set(True)
        dialog._save()
        assert errors
        assert settings_store.settings.ancs_enabled is False
    finally:
        dialog.close()


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


def test_supabase_url_is_accepted_without_the_sb_prefix(hidden_root, settings_store):
    settings_store.update(
        supabase_enabled=True, supabase_url="sb://demo-project.supabase.co"
    )
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        # The field shows the project URL people actually paste, not sb://.
        assert dialog.supabase_url_var.get() == "https://demo-project.supabase.co"
        dialog.supabase_url_var.set("https://other-project.supabase.co/")
        assert dialog._candidate()["supabase_url"] == "sb://other-project.supabase.co"
    finally:
        dialog.close()


def test_connection_tab_text_inputs_grow_to_the_right_edge(hidden_root, settings_store):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        tab = dialog.notebook.nametowidget(dialog.notebook.tabs()[0])
        entries = [w for w in tab.winfo_children() if w.winfo_class() == "TEntry"]
        assert entries
        # Labels hold the left edge; every input starts in the same column and
        # stretches east so it reaches the window's right edge.
        assert int(tab.columnconfigure(1)["weight"]) == 1
        for entry in entries:
            info = entry.grid_info()
            assert int(info["column"]) == 1
            assert set(str(info["sticky"])) == {"e", "w"}
    finally:
        dialog.close()


def _status_label(tab, var):
    for child in tab.winfo_children():
        if child.winfo_class() != "TLabel":
            continue
        try:
            if str(child.cget("textvariable")) == str(var):
                return child
        except tk.TclError:
            continue
    raise AssertionError("status label not found")


def test_one_line_status_collapses_and_truncates():
    assert _one_line_status("a\n  b") == "a b"
    assert _one_line_status("x" * STATUS_MAX_CHARS) == "x" * STATUS_MAX_CHARS
    clipped = _one_line_status("x" * (STATUS_MAX_CHARS + 10))
    assert len(clipped) == STATUS_MAX_CHARS
    assert clipped.endswith("…")


def test_bluetooth_sections_share_the_same_layout(hidden_root, settings_store):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        tab = dialog.notebook.nametowidget(dialog.notebook.tabs()[1])
        audio_actions = dialog.bluetooth_refresh_button.master
        ancs_actions = dialog.ancs_refresh_button.master

        def row(widget):
            return int(widget.grid_info()["row"])

        # Both sections repeat: checkbox, combo, actions row, status.
        assert row(dialog.bluetooth_combo) == row(dialog.bluetooth_check) + 1
        assert row(audio_actions) == row(dialog.bluetooth_combo) + 1
        assert row(dialog.ancs_combo) == row(dialog.ancs_check) + 1
        assert row(ancs_actions) == row(dialog.ancs_combo) + 1
        assert row(ancs_actions) - row(dialog.ancs_check) == (
            row(audio_actions) - row(dialog.bluetooth_check)
        )

        # The adapter's Refresh moved below the combo, next to Restart receiver.
        assert dialog.ancs_refresh_button.winfo_parent() == (
            dialog.ancs_restart_button.winfo_parent()
        )
        assert dialog.ancs_refresh_button.winfo_parent() != dialog.ancs_combo.winfo_parent()
        assert dialog.ancs_refresh_button.grid_info()["column"] == 0
        assert dialog.ancs_restart_button.grid_info()["column"] == 1

        # Both status labels are single-line (no wrap) and sit on the same row.
        audio_status = _status_label(tab, dialog.bluetooth_status_display_var)
        ancs_status = _status_label(tab, dialog.ancs_status_display_var)
        assert str(audio_status.cget("wraplength")) in ("", "0")
        assert str(ancs_status.cget("wraplength")) in ("", "0")
        assert row(audio_status) - row(dialog.bluetooth_check) == (
            row(ancs_status) - row(dialog.ancs_check)
        )
    finally:
        dialog.close()


def test_long_bluetooth_status_stays_on_one_line(hidden_root, settings_store):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        long_text = "Bluetooth error: " + "x" * 200
        dialog.bluetooth_status_var.set(long_text)
        dialog.ancs_status_var.set(long_text)
        # The full message stays available in the source variable...
        assert dialog.bluetooth_status_var.get() == long_text
        assert dialog.ancs_status_var.get() == long_text
        # ...while the label only shows a clipped single line.
        for display in (
            dialog.bluetooth_status_display_var,
            dialog.ancs_status_display_var,
        ):
            assert len(display.get()) == STATUS_MAX_CHARS
            assert display.get().endswith("…")
    finally:
        dialog.close()


def test_test_voice_button_sits_on_the_last_configuration_row(
    hidden_root, settings_store
):
    dialog = SettingsDialog(
        hidden_root, settings_store=settings_store, controller=FakeController()
    )
    try:
        tab_id = next(
            tab
            for tab in dialog.notebook.tabs()
            if dialog.notebook.tab(tab, "text") == "Notifications"
        )
        tab = dialog.notebook.nametowidget(tab_id)
        buttons = [w for w in tab.winfo_children() if w.winfo_class() == "TButton"]
        assert [button.cget("text") for button in buttons] == ["Test voice"]
        rows = [
            int(child.grid_info()["row"])
            for child in tab.winfo_children()
            if child.grid_info()
        ]
        assert int(buttons[0].grid_info()["row"]) == max(rows)
    finally:
        dialog.close()
