"""`barkpc:` URI scheme: per-user registration plus a strict parser.

Toast click-through uses this scheme for messages without an `http(s)` URL, so a
click can restore a hidden window.  Registration is HKCU-only and idempotent.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("bark_pc.uri_scheme")

SCHEME = "barkpc"
CLASSES_PATH = rf"Software\Classes\{SCHEME}"
PROTOCOL_DESCRIPTION = "URL:Bark Desktop Client"


def parse_barkpc_uri(uri: str) -> dict:
    """Parse `barkpc:open`, `barkpc:open?id=<n>` and `barkpc:setup`.

    Unknown or malformed URIs yield `{"command": "focus", "valid": False}` so the
    caller can focus the main window and log a warning, never raise a dialog.
    """
    result: dict = {"command": "focus", "notification_id": None, "valid": False}
    text = (uri or "").strip().strip('"')
    if not text:
        result["reason"] = "empty URI"
        return result
    parts = urlsplit(text)
    if parts.scheme.lower() != SCHEME:
        result["reason"] = f"unexpected scheme: {parts.scheme!r}"
        return result
    # Windows hands us `barkpc:open?id=3`, which urlsplit sees as path "open".
    action = (parts.path or "").strip("/").lower()
    if action == "" and parts.netloc:
        action = parts.netloc.lower()
    if action == "open":
        result.update(command="open", valid=True)
        query = parse_qs(parts.query)
        raw_id = (query.get("id") or [None])[0]
        if raw_id is not None:
            try:
                result["notification_id"] = int(raw_id)
            except (TypeError, ValueError):
                result["reason"] = "notification id is not a number"
                result["valid"] = False
        return result
    if action == "setup":
        result.update(command="setup", valid=True)
        return result
    result["reason"] = f"unknown command: {action!r}"
    return result


class WindowsUriHandler:
    """Registers/removes `HKCU\\Software\\Classes\\barkpc`."""

    def __init__(self, *, key_path: str = CLASSES_PATH):
        self.key_path = key_path

    def register(self, exe_path: str | Path) -> None:
        if sys.platform != "win32":  # pragma: no cover - non-Windows dev runs
            return
        import winreg

        exe = Path(exe_path).resolve()
        command = f'"{exe}" "%1"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.key_path) as key:
            winreg.SetValueEx(key, None, 0, winreg.REG_SZ, PROTOCOL_DESCRIPTION)
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{self.key_path}\DefaultIcon"
        ) as icon_key:
            winreg.SetValueEx(icon_key, None, 0, winreg.REG_SZ, f'"{exe}",0')
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{self.key_path}\shell\open\command"
        ) as command_key:
            winreg.SetValueEx(command_key, None, 0, winreg.REG_SZ, command)
        logger.info("registered the barkpc: URI scheme")

    def unregister(self) -> None:
        if sys.platform != "win32":  # pragma: no cover
            return
        import winreg

        def delete_tree(path: str) -> None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
                    index = 0
                    children: list[str] = []
                    while True:
                        try:
                            children.append(winreg.EnumKey(key, index))
                            index += 1
                        except OSError:
                            break
                for child in children:
                    delete_tree(rf"{path}\{child}")
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                return
            except OSError as exc:  # pragma: no cover - defensive
                logger.debug("could not delete %s: %s", path, exc)

        delete_tree(self.key_path)
        logger.info("removed the barkpc: URI scheme")

    def parse(self, uri: str) -> dict:
        return parse_barkpc_uri(uri)


def is_registered(key_path: str = CLASSES_PATH) -> bool:
    if sys.platform != "win32":  # pragma: no cover
        return False
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_READ).Close()
        return True
    except OSError:
        return False
