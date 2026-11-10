"""Tk root lifecycle: hidden window, DPI setup, thread marshalling, shutdown.

Tk must own the main thread.  The receiver and image workers reach the UI only
through `post()` (which delegates to `core.bus.post_to_ui`).
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from ..core import bus
from . import icons, theme

logger = logging.getLogger("bark_pc.ui.shell")


class AppShell:
    """Owns the Tk root and the (initially hidden) main window."""

    def __init__(self, *, title: str = "Bark Desktop Client", on_close_to_tray=None):
        self.title = title
        self.on_close_to_tray = on_close_to_tray
        self.root: tk.Tk | None = None
        self.marshaller: bus.UiMarshaller | None = None
        self.factor = 1.0
        self._icon_images: list = []
        self._windows: dict[str, object] = {}
        self._shutdown_callbacks: list = []
        self._closing = False

    # -- lifecycle --------------------------------------------------------- #
    def create(self) -> tk.Tk:
        theme.enable_dpi_awareness()
        root = tk.Tk()
        root.title(self.title)
        root.withdraw()
        self.factor = theme.apply_theme(root)
        # Must happen after DPI awareness so the sizes are not rescaled by Tk.
        self._icon_images = icons.apply_window_icon(root)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root = root
        self.marshaller = bus.UiMarshaller(root)
        bus.configure_ui_scheduler(self.marshaller)
        logger.debug("Tk root created (scale %.2f)", self.factor)
        return root

    def register_window(self, name: str, window) -> None:
        self._windows[name] = window

    def window(self, name: str):
        return self._windows.get(name)

    def post(self, func) -> None:
        if self.marshaller is not None:
            self.marshaller(func)
        else:  # pragma: no cover - before the UI exists
            bus.post_to_ui(func)

    def on_shutdown(self, callback) -> None:
        self._shutdown_callbacks.append(callback)

    # -- visibility -------------------------------------------------------- #
    def show(self) -> None:
        if self.root is None:
            return
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        if self.root is None:
            return
        self.root.withdraw()
        if self.on_close_to_tray is not None:
            try:
                self.on_close_to_tray()
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("close-to-tray callback failed")

    # -- main loop --------------------------------------------------------- #
    def run(self) -> None:
        assert self.root is not None
        self.root.mainloop()

    def stop(self) -> None:
        if self.root is None or self._closing:
            return
        self._closing = True
        for callback in self._shutdown_callbacks:
            try:
                callback()
            except Exception:  # pragma: no cover - shutdown must continue
                logger.exception("shutdown callback failed")
        if self.marshaller is not None:
            self.marshaller.close()
        bus.configure_ui_scheduler(None)
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # pragma: no cover - already destroyed
            pass
        self.root = None
        logger.info("UI stopped")

    def quit_application(self, confirm: bool = True, on_quit=None) -> None:
        if confirm and self.root is not None:
            answer = messagebox.askyesno(
                "Quit Bark Desktop Client",
                "Quit the app? Push notifications will stop until it is started again.",
                parent=self.root,
            )
            if not answer:
                return
        if on_quit is not None:
            on_quit()
        self.stop()

    # -- shared widgets ---------------------------------------------------- #
    def status_bar(self, parent) -> ttk.Label:
        label = ttk.Label(parent, text="", style="Status.TLabel", anchor="w")
        return label
