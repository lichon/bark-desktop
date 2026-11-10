"""Spike T2 — frozen-import smoke test.

Run from inside the PyInstaller onedir build (or as `python -m
bark_pc.tools.spike_frozen_imports`) to prove that `pypush`, `winrt.*`,
`pystray`, Tk and Pillow are all importable without a network install.
The outcome is appended to `docs/validation/spike_T2.md`.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

MODULES = [
    "tkinter",
    "tkinter.ttk",
    "PIL",
    "PIL.ImageTk",
    "pystray",
    "pystray._win32",
    "windows_toasts",
    "winrt.windows.ui.notifications",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.data.xml.dom",
    "segno",
    "httpx",
    "anyio",
    "anyio._backends._asyncio",
    "cryptography",
    "pypush",
    "pypush.apns",
]


def main(argv: list[str] | None = None) -> int:
    report = Path("docs/validation/spike_T2.md")
    if argv:
        report = Path(argv[0])
    failures: list[tuple[str, str]] = []
    for name in MODULES:
        try:
            __import__(name)
            print(f"ok   {name}")
        except Exception as exc:
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL {name}: {exc}")

    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"## {datetime.now().isoformat(timespec='seconds')} — frozen import smoke test",
        f"- frozen: {bool(getattr(sys, 'frozen', False))}",
        f"- executable: `{sys.executable}`",
        f"- modules checked: {len(MODULES)}",
        f"- failures: {len(failures)}",
    ]
    for name, error in failures:
        lines.append(f"  - `{name}`: {error}")
    if failures:
        lines.append("- required hidden imports: " + ", ".join(name for name, _ in failures))
    lines.append("")
    with report.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
