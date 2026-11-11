"""Tray menu surface: actions stay top-level and wired to the controller."""

from __future__ import annotations

import pystray

from bark_pc.platform.windows.tray import TrayController

ACTION_NAMES = (
    "open_history",
    "test_notification",
    "toggle_tts",
    "copy_key",
    "settings",
    "setup",
    "show_log_folder",
    "quit",
)


def _controller() -> tuple[TrayController, list[str]]:
    calls: list[str] = []
    actions = {name: (lambda name=name: calls.append(name)) for name in ACTION_NAMES}
    controller = TrayController(actions=actions, summary=lambda: "Bark Desktop Client")
    return controller, calls


def _items(controller: TrayController) -> list[pystray.MenuItem]:
    menu = controller._build_menu(pystray)
    return [item for item in menu if item is not pystray.Menu.SEPARATOR]


def test_show_log_folder_is_a_top_level_item() -> None:
    controller, _calls = _controller()
    labels = [item.text for item in _items(controller)]

    assert "Logs…" in labels
    assert pystray.Menu.SEPARATOR not in _items(controller)


def test_show_log_folder_invokes_the_registered_action() -> None:
    controller, calls = _controller()
    item = next(item for item in _items(controller) if item.text == "Logs…")

    item(None)

    assert calls == ["show_log_folder"]
