"""Window icon: the tray artwork, applied to Tk windows.

Tk windows show the default Tk feather unless an icon is set explicitly, so the
main window (and every dialog, via the default icon) reuses the tray rendering
at each size Windows asks for: 16 px for the title bar, 32 px for the taskbar
and Alt+Tab, and larger images for high-DPI and large-icon views.
"""

from __future__ import annotations

import logging

from ..core.models import ConnectionStatus
from ..platform.windows.tray import render_icon

logger = logging.getLogger("bark_pc.ui.icons")

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def window_icon_images(master=None, status: ConnectionStatus = ConnectionStatus.ACTIVE) -> list:
    """Render one `PhotoImage` per icon size; `master` keeps Tk ownership right."""
    from PIL import ImageTk  # noqa: PLC0415 - only available with Tk support

    return [
        ImageTk.PhotoImage(render_icon(status, size=size), master=master)
        for size in ICON_SIZES
    ]


def apply_window_icon(root, status: ConnectionStatus = ConnectionStatus.ACTIVE) -> list:
    """Set the window icon and make it the default for every toplevel.

    Returns the rendered images: Tk only keeps a weak reference, so the caller
    must hold on to them for as long as the windows live or the icon reverts.
    """
    try:
        images = window_icon_images(root, status)
        root.iconphoto(True, *images)
    except Exception:  # pragma: no cover - a wrong icon must never break startup
        logger.exception("could not apply the window icon")
        return []
    return images
