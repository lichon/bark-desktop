"""Tray icon: status colours, unread badge, dynamic menu, Explorer recovery."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from PIL import Image, ImageDraw

from ...core.models import ConnectionStatus

logger = logging.getLogger("bark_pc.tray")

ICON_SIZE = 64
BADGE_MAX = 9

STATUS_COLORS = {
    ConnectionStatus.STARTING: (128, 128, 128, 255),
    ConnectionStatus.CONNECTING: (240, 180, 41, 255),
    ConnectionStatus.ACTIVE: (37, 99, 235, 255),
    ConnectionStatus.DEGRADED: (240, 180, 41, 255),
    ConnectionStatus.INVALID: (220, 38, 38, 255),
    ConnectionStatus.SILENCED: (79, 70, 229, 255),
}


def render_icon(
    status: ConnectionStatus | str = ConnectionStatus.STARTING,
    *,
    unread: int = 0,
    silenced: bool = False,
    size: int = ICON_SIZE,
) -> Image.Image:
    """Draw a status-coloured ring with an optional unread badge.

    The interior stays transparent so the icon shows only the ring.
    """
    status = ConnectionStatus(status) if isinstance(status, str) else status
    color = STATUS_COLORS.get(status, STATUS_COLORS[ConnectionStatus.STARTING])
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # Status ring so the colour reads at 16 px.
    draw.ellipse((2, 2, size - 3, size - 3), outline=color, width=max(2, size // 20))
    if silenced:
        draw.line(
            (size // 4, size // 4, size - size // 4, size - size // 4),
            fill=(17, 24, 39, 255),
            width=max(2, size // 12),
        )
    if unread > 0:
        radius = size // 4
        cx, cy = size - radius - 2, radius + 2
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(220, 38, 38, 255))
        label = f"{BADGE_MAX}+" if unread > BADGE_MAX else str(unread)
        try:
            from PIL import ImageFont

            font = ImageFont.load_default(size=int(radius * 1.4))
        except Exception:  # pragma: no cover - older Pillow
            font = None
        draw.text((cx, cy), label, fill=(255, 255, 255, 255), anchor="mm", font=font)
    return image


def format_badge(count: int) -> str:
    return f"{BADGE_MAX}+" if count > BADGE_MAX else str(max(0, count))


class TrayController:
    """Wraps `pystray.Icon`; menu text is derived from live callables."""

    def __init__(
        self,
        *,
        actions: dict[str, Callable[[], None]],
        summary: Callable[[], str] | None = None,
        app_name: str = "Bark Desktop Client",
        on_copy_key: Callable[[], str] | None = None,
        checks: dict[str, Callable[[], bool]] | None = None,
    ):
        self.actions = actions
        self.summary = summary or (lambda: "Bark Desktop Client")
        self.app_name = app_name
        self.on_copy_key = on_copy_key
        self.checks = checks or {}
        self.status = ConnectionStatus.STARTING
        self.unread = 0
        self.silenced = False
        self._icon = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle --------------------------------------------------------- #
    def start(self) -> None:
        if self._icon is not None:
            return
        try:
            import pystray
        except Exception as exc:  # pragma: no cover - headless environments
            logger.warning("tray unavailable: %s", exc)
            return
        self._pystray = pystray
        self._icon = pystray.Icon(
            "bark_pc",
            icon=render_icon(self.status, unread=self.unread, silenced=self.silenced),
            title=self._tooltip(),
            menu=self._build_menu(pystray),
        )
        self._thread = threading.Thread(target=self._run, name="bark-tray", daemon=True)
        self._thread.start()
        threading.Thread(
            target=self._shell_watchdog, name="bark-tray-watchdog", daemon=True
        ).start()

    def _run(self) -> None:  # pragma: no cover - requires a desktop session
        try:
            self._icon.run()
        except Exception as exc:
            logger.warning("tray loop stopped: %s", exc)

    def _shell_watchdog(self) -> None:  # pragma: no cover - requires Explorer
        """Re-add the icon after Explorer restarts (which drops the tray icon)."""
        while not self._stop.wait(60.0):
            if self._icon is None:
                continue
            try:
                self._icon.visible = True
                logger.debug("tray icon re-asserted")
            except Exception as exc:
                logger.debug("tray re-assert failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:  # pragma: no cover - already stopped
                pass
            self._icon = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # -- menu -------------------------------------------------------------- #
    def _build_menu(self, pystray):
        item = pystray.MenuItem
        toggle_tts = item(
            "Speak notifications",
            lambda: self._call("toggle_tts"),
            checked=self._check("toggle_tts"),
        )
        return pystray.Menu(
            item(lambda _item: self.summary(), self._noop, enabled=False),
            pystray.Menu.SEPARATOR,
            item("Messages", lambda: self._call("open_history"), default=True),
            item("Test notification", lambda: self._call("test_notification")),
            toggle_tts,
            item("Copy device key", lambda: self._call("copy_key")),
            pystray.Menu.SEPARATOR,
            item("Settings…", lambda: self._call("settings")),
            item("Setup…", lambda: self._call("setup")),
            item("Logs…", lambda: self._call("show_log_folder")),
            pystray.Menu.SEPARATOR,
            item("Quit", lambda: self._call("quit")),
        )

    def _check(self, name: str):
        """pystray calls `checked(item)` whenever the menu is re-rendered."""
        probe = self.checks.get(name)
        if probe is None:
            return None
        return lambda _item: bool(probe())

    @staticmethod
    def _noop(*_args) -> None:
        return None

    def _call(self, name: str) -> None:
        action = self.actions.get(name)
        if action is None:
            logger.debug("no action registered for %s", name)
            return
        try:
            action()
        except Exception:  # pragma: no cover - menu callbacks must not raise
            logger.exception("tray action %s failed", name)

    # -- state ------------------------------------------------------------- #
    def _tooltip(self) -> str:
        label = {
            ConnectionStatus.STARTING: "Starting",
            ConnectionStatus.CONNECTING: "Connecting…",
            ConnectionStatus.ACTIVE: "Connected",
            ConnectionStatus.DEGRADED: "Reconnecting…",
            ConnectionStatus.INVALID: "Action required",
            ConnectionStatus.SILENCED: "Silenced",
        }.get(self.status, "Bark")
        return f"{self.app_name} — {label}"

    def _refresh(self) -> None:
        if self._icon is None:
            return
        try:
            self._icon.icon = render_icon(self.status, unread=self.unread, silenced=self.silenced)
            self._icon.title = self._tooltip()
            self._icon.update_menu()
        except Exception as exc:  # pragma: no cover - shell not ready
            logger.debug("tray refresh failed: %s", exc)

    def set_status(self, status: ConnectionStatus | str, reason: str | None = None) -> None:
        self.status = ConnectionStatus(status) if isinstance(status, str) else status
        self.silenced = self.status == ConnectionStatus.SILENCED
        self._refresh()

    def set_unread(self, count: int) -> None:
        self.unread = max(0, int(count))
        self._refresh()

    def set_silenced(self, silenced: bool) -> None:
        self.silenced = bool(silenced)
        if self.silenced:
            self.status = ConnectionStatus.SILENCED
        self._refresh()

    def notify(self, title: str, message: str) -> None:
        if self._icon is None:
            logger.info("%s: %s", title, message)
            return
        try:  # pragma: no cover - requires a desktop session
            self._icon.notify(message, title)
        except Exception as exc:
            logger.debug("tray notification failed: %s", exc)
