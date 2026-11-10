"""ttk theme, DPI awareness and font scaling for the Tk surfaces."""

from __future__ import annotations

import logging

from ..platform.windows.dpi import enable_dpi_awareness, scale_factor

logger = logging.getLogger("bark_pc.ui.theme")

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"
BASE_FONT_SIZE = 10

__all__ = [
    "FONT_FAMILY",
    "MONO_FAMILY",
    "BASE_FONT_SIZE",
    "apply_theme",
    "enable_dpi_awareness",
    "scale_factor",
    "px",
]


def apply_theme(root) -> float:
    """Apply a small flat ttk theme and return the scale factor used."""
    import tkinter.font as tkfont
    from tkinter import ttk

    factor = scale_factor()
    try:
        root.tk.call("tk", "scaling", factor * 1.3333)
    except Exception:  # pragma: no cover
        pass

    base = tkfont.nametofont("TkDefaultFont")
    base.configure(family=FONT_FAMILY, size=BASE_FONT_SIZE)
    text = tkfont.nametofont("TkTextFont")
    text.configure(family=FONT_FAMILY, size=BASE_FONT_SIZE)
    fixed = tkfont.nametofont("TkFixedFont")
    fixed.configure(family=MONO_FAMILY, size=BASE_FONT_SIZE)

    style = ttk.Style(root)
    try:
        style.theme_use("vista")
    except Exception:  # pragma: no cover - non-Windows
        try:
            style.theme_use("clam")
        except Exception:
            pass

    accent = "#2563eb"
    style.configure("TButton", padding=(10, 4))
    style.configure("Accent.TButton", foreground="#ffffff", background=accent)
    style.map("Accent.TButton", background=[("active", "#1d4ed8")])
    style.configure("Title.TLabel", font=(FONT_FAMILY, BASE_FONT_SIZE + 6, "bold"))
    style.configure("Subtitle.TLabel", font=(FONT_FAMILY, BASE_FONT_SIZE + 1))
    style.configure("Muted.TLabel", foreground="#6b7280")
    style.configure("Status.TLabel", foreground="#374151")
    style.configure("Danger.TLabel", foreground="#b91c1c")
    style.configure("Treeview", rowheight=int(24 * factor))
    style.configure("Unread.Treeview", font=(FONT_FAMILY, BASE_FONT_SIZE, "bold"))
    return factor


def px(value: int, factor: float | None = None) -> int:
    return int(round(value * (factor if factor is not None else scale_factor())))
