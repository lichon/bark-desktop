"""Per-user autostart via the HKCU Run key (no admin rights, no shortcuts)."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("bark_pc.autostart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "BarkDesktopClient"

DECISION_REPAIR = "repair"
DECISION_DISABLE = "disable"
DECISION_IGNORE = "ignore"


def quote_path(path: str | Path) -> str:
    """Return one quoted absolute path (the Run value format)."""
    text = str(path or "").strip().strip('"')
    if not text:
        raise ValueError("autostart path is empty")
    if not os.path.isabs(text):
        raise ValueError(f"autostart path must be absolute: {text}")
    return f'"{text}"'


def unquote_path(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    return text


def repair_decision(
    *,
    enabled: bool,
    stored_path: str | None,
    current_exe: str | Path | None,
) -> str:
    """Decide what the UI should do with a stored autostart entry."""
    if not enabled:
        return DECISION_IGNORE
    if not stored_path:
        return DECISION_REPAIR
    if current_exe is None:
        return DECISION_IGNORE
    if os.path.normcase(os.path.abspath(stored_path)) != os.path.normcase(
        os.path.abspath(str(current_exe))
    ):
        return DECISION_REPAIR
    return DECISION_IGNORE


class WindowsAutostart:
    """Thin wrapper over `winreg` with an injectable key path for tests."""

    def __init__(
        self,
        *,
        key_path: str = RUN_KEY,
        value_name: str = VALUE_NAME,
        logger_: logging.Logger | None = None,
    ):
        self.key_path = key_path
        self.value_name = value_name
        self.logger = logger_ or logger

    # -- low level --------------------------------------------------------- #
    def _open(self, *, write: bool = False):
        import winreg

        access = winreg.KEY_READ | (winreg.KEY_WRITE if write else 0)
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key_path, 0, access)

    def _read_raw(self) -> str | None:
        if sys.platform != "win32":  # pragma: no cover - non-Windows dev runs
            return None
        import winreg

        try:
            with self._open() as key:
                value, _kind = winreg.QueryValueEx(key, self.value_name)
                return str(value)
        except FileNotFoundError:
            return None
        except OSError as exc:  # pragma: no cover - permission problems
            self.logger.warning("could not read the Run key: %s", exc)
            return None

    # -- public API -------------------------------------------------------- #
    def is_enabled(self) -> bool:
        return bool(self._read_raw())

    def stored_path(self) -> str | None:
        return unquote_path(self._read_raw())

    def enable(self, exe_path: str | Path | None = None) -> str:
        if exe_path is None:
            exe_path = sys.executable
        value = quote_path(exe_path)
        import winreg

        with self._open(write=True) as key:
            winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, value)
        self.logger.info("autostart enabled")
        return value

    def disable(self) -> None:
        import winreg

        try:
            with self._open(write=True) as key:
                winreg.DeleteValue(key, self.value_name)
            self.logger.info("autostart disabled")
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - defensive
            self.logger.warning("could not delete the Run value: %s", exc)

    def decision(self, current_exe: str | Path | None = None) -> str:
        return repair_decision(
            enabled=self.is_enabled(),
            stored_path=self.stored_path(),
            current_exe=current_exe or sys.executable,
        )
