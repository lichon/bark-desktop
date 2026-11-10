"""Spike T0 — do toasts display from an unpackaged, portable exe?

    python -m bark_pc.tools.spike_toast [--image path.png]

Shows a text toast, then (when an image is given) a hero-image toast, and prints
what the WinRT API reported.  Visual confirmation on a clean Windows 11 VM and on
Windows 10 21H2 is required before the UI work counts as done; record the outcome
in `docs/validation/spike_T0.md`.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from bark_pc.core.models import Notification, now_ms
from bark_pc.platform.windows.toast import WindowsToastNotifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="image to show as a hero image")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("docs/validation/spike_T0.md"),
        help="file to append the outcome to",
    )
    args = parser.parse_args(argv)

    image_path = args.image if args.image and args.image.exists() else None
    notifier = WindowsToastNotifier(
        image_resolver=lambda _n: image_path,
    )
    shown = notifier.show(
        Notification(
            title="Bark Desktop Client",
            body="Spike T0: this toast came from an unpackaged process.",
            received_at=now_ms(),
        )
    )
    print(f"text toast request returned {shown!r}")
    if image_path is not None:
        shown_image = notifier.show(
            Notification(
                title="Bark Desktop Client",
                body="Spike T0: hero image test.",
                image_url=str(image_path),
                received_at=now_ms(),
            )
        )
        print(f"hero-image toast request returned {shown_image!r}")

    report = args.report
    report.parent.mkdir(parents=True, exist_ok=True)
    with report.open("a", encoding="utf-8") as handle:
        handle.write(
            f"## {datetime.now().isoformat(timespec='seconds')} — spike run\n"
            f"- frozen: {bool(getattr(sys, 'frozen', False))}\n"
            f"- executable: `{sys.executable}`\n"
            f"- text toast API result: {shown!r}\n"
            f"- hero image used: {image_path or 'none'}\n"
            f"- visual confirmation: PENDING (fill in after looking at the screen)\n\n"
        )
    print("waiting for the toast to be visible…")
    time.sleep(max(1.0, args.seconds))
    return 0 if shown else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
