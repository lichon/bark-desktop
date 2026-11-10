"""Build the portable `--onedir --windowed` bundle with PyInstaller.

    python -m bark_pc.tools.build_portable [--no-zip]

Produces `dist/BarkDesktopClient/` containing `bark_pc.exe` and `_internal/`,
copies `NOTICE`/`README.md` next to the exe, and zips the folder for
distribution.  Hidden imports come from spike T2 (WinRT namespace packages and
the pypush/anyio backends that are imported dynamically).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

APP_NAME = "BarkDesktopClient"
ENTRY = Path("bark_pc/launcher.py")
ROOT = Path(__file__).resolve().parent.parent.parent

HIDDEN_IMPORTS = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "winrt.windows.ui.notifications",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.data.xml.dom",
    "pypush.apns",
    "pypush.apns.albert",
    "pypush.apns.lifecycle",
    "pypush.apns.transport",
    "pystray._win32",
    "anyio._backends._asyncio",
    "PIL._tkinter_finder",
]

COLLECT_ALL = ["pystray", "windows_toasts", "winrt", "pypush", "segno"]


def build(zip_output: bool = True) -> Path:
    dist_dir = ROOT / "dist"
    build_dir = ROOT / "build"
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
    ]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for package in COLLECT_ALL:
        command += ["--collect-all", package]
    icon = ROOT / "bark_pc" / "assets" / "icons" / "app.ico"
    if not icon.exists():
        icon = icon.with_suffix(".png")  # PyInstaller converts a single PNG, badly
    if icon.exists():
        command += ["--icon", str(icon)]
    command.append(str(ROOT / ENTRY))

    print("running:", " ".join(command))
    subprocess.run(command, check=True, cwd=ROOT)

    bundle = dist_dir / APP_NAME
    for extra in ("NOTICE", "README.md", "LICENSE"):
        source = ROOT / extra
        if source.exists():
            shutil.copy2(source, bundle / extra)
    (bundle / "docs").mkdir(exist_ok=True)
    quickstart = ROOT / "specs" / "001-bark-desktop-client" / "quickstart.md"
    if quickstart.exists():
        shutil.copy2(quickstart, bundle / "docs" / "quickstart.md")

    if zip_output:
        archive = dist_dir / f"{APP_NAME}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            for path in bundle.rglob("*"):
                if path.is_file():
                    handle.write(path, path.relative_to(bundle.parent))
        print(f"wrote {archive}")
    print(f"portable build at {bundle}")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)
    build(zip_output=not args.no_zip)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
