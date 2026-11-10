"""Generate the tray/toast icon assets (16/20/24/32/64 px PNGs)."""

from __future__ import annotations

import sys
from pathlib import Path

from bark_pc.core.models import ConnectionStatus
from bark_pc.platform.windows.tray import render_icon

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
SIZES = (16, 20, 24, 32, 64)
# The .ico carries its own sizes because Windows scales a single frame badly:
# the ring is redrawn per size instead of resampled.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        image = render_icon(ConnectionStatus.ACTIVE, size=size)
        target = ICON_DIR / f"{size}.png"
        image.save(target, format="PNG")
        print(f"wrote {target}")
    # Copy the 64 px icon as the application icon used by PyInstaller.
    render_icon(ConnectionStatus.ACTIVE, size=64).save(ICON_DIR / "app.png", format="PNG")
    # Shell/taskbar icon embedded in the exe (Explorer, Start menu, shortcuts).
    frames = [render_icon(ConnectionStatus.ACTIVE, size=size) for size in ICO_SIZES]
    target = ICON_DIR / "app.ico"
    frames[-1].save(
        target,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=frames[:-1],
    )
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
