"""DPI awareness helpers (Win32 only, kept out of the UI layer)."""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger("bark_pc.dpi")


def enable_dpi_awareness() -> int:
    """Per-monitor DPI awareness; must run before Tk creates its root window."""
    if sys.platform != "win32":
        return 0
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        return int(ctypes.windll.shcore.SetProcessDpiAwareness(2))
    except Exception as exc:  # pragma: no cover - older Windows
        logger.debug("per-monitor DPI awareness unavailable: %s", exc)
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # pragma: no cover - defensive
            pass
        return 0


def scale_factor() -> float:
    """Primary monitor scale factor (1.0 at 96 DPI)."""
    if sys.platform != "win32":
        return 1.0
    try:
        dc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, dc)
        return max(1.0, dpi / 96.0)
    except Exception:  # pragma: no cover - defensive
        return 1.0
