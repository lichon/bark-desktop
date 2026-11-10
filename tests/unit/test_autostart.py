"""Autostart toggling and stale-path detection (HKCU test mirror key)."""

from __future__ import annotations

import os
import sys

import pytest

from bark_pc.platform.windows.autostart import (
    DECISION_DISABLE,
    DECISION_IGNORE,
    DECISION_REPAIR,
    WindowsAutostart,
    quote_path,
    repair_decision,
    unquote_path,
)

TEST_KEY = r"Software\BarkDesktopClient\TestRun"

pytestmark = pytest.mark.windows_only


@pytest.fixture
def autostart_key():
    if sys.platform != "win32":
        pytest.skip("winreg is Windows-only")
    import winreg

    winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY).Close()
    try:
        yield TEST_KEY
    finally:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY, 0, winreg.KEY_WRITE)
            try:
                winreg.DeleteValue(key, "BarkDesktopClient")
            except FileNotFoundError:
                pass
            finally:
                key.Close()
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        except FileNotFoundError:
            pass


def test_enable_writes_a_quoted_absolute_path(autostart_key):
    autostart = WindowsAutostart(key_path=autostart_key)
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "notepad.exe")

    value = autostart.enable(exe)

    assert value == f'"{exe}"'
    assert autostart.is_enabled() is True
    assert autostart.stored_path() == exe


def test_disable_removes_the_value(autostart_key):
    autostart = WindowsAutostart(key_path=autostart_key)
    autostart.enable(sys.executable)
    assert autostart.is_enabled() is True

    autostart.disable()

    assert autostart.is_enabled() is False
    assert autostart.stored_path() is None


def test_disable_is_idempotent(autostart_key):
    WindowsAutostart(key_path=autostart_key).disable()
    WindowsAutostart(key_path=autostart_key).disable()


def test_default_enable_uses_the_running_interpreter(autostart_key):
    autostart = WindowsAutostart(key_path=autostart_key)
    autostart.enable()
    stored = autostart.stored_path()
    assert os.path.isabs(stored)
    assert os.path.normcase(stored) == os.path.normcase(os.path.abspath(sys.executable))


def test_empty_and_relative_paths_are_never_written(autostart_key):
    autostart = WindowsAutostart(key_path=autostart_key)
    for bad in ("", "   ", r"relative\bark_pc.exe", "bark_pc.exe"):
        with pytest.raises(ValueError):
            autostart.enable(bad)
    assert autostart.is_enabled() is False


def test_quote_and_unquote_are_inverse():
    value = quote_path(r"C:\Apps\Bark Desktop\bark_pc.exe")
    assert value.startswith('"') and value.endswith('"')
    assert unquote_path(value) == r"C:\Apps\Bark Desktop\bark_pc.exe"
    assert unquote_path(None) is None


def test_repair_decision_detects_a_moved_folder():
    assert (
        repair_decision(
            enabled=True,
            stored_path=r"C:\Old\BarkDesktopClient\bark_pc.exe",
            current_exe=r"D:\New\BarkDesktopClient\bark_pc.exe",
        )
        == DECISION_REPAIR
    )


def test_repair_decision_ignores_matching_paths():
    assert (
        repair_decision(
            enabled=True,
            stored_path=r"C:\Apps\bark_pc.exe",
            current_exe=r"C:\Apps\bark_pc.exe",
        )
        == DECISION_IGNORE
    )


def test_repair_decision_when_disabled_or_missing():
    assert (
        repair_decision(enabled=False, stored_path="C:\\x.exe", current_exe="D:\\y.exe")
        == DECISION_IGNORE
    )
    assert (
        repair_decision(enabled=True, stored_path=None, current_exe="D:\\y.exe") == DECISION_REPAIR
    )
    assert DECISION_DISABLE == "disable"


def test_stored_path_reads_a_manually_written_value(autostart_key):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart_key, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "BarkDesktopClient", 0, winreg.REG_SZ, r"C:\manual\path.exe")
    assert WindowsAutostart(key_path=autostart_key).stored_path() == r"C:\manual\path.exe"
