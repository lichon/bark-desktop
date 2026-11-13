"""Build the portable desktop bundle for the host platform with PyInstaller.

    python -m bark_pc.tools.build_portable [--target auto|windows|macos|linux]
                                           [--archive auto|zip|tar.gz|none]

Every platform gets a self-contained, installer-free bundle in ``dist/``:

===========  ==============================  ==========================
Platform     Bundle                          Archive
===========  ==============================  ==========================
Windows      ``dist/BarkDesktopClient/``     ``.zip``
macOS        ``dist/BarkDesktopClient.app``  ``.tar.gz``
Linux        ``dist/BarkDesktopClient/``     ``.tar.gz``
===========  ==============================  ==========================

The hidden imports, tray backend and launcher/icon format are selected per
platform, so the same command works from a Windows, macOS or Linux checkout.
POSIX archives use tar so the executable bit and symlinks survive; Windows uses
zip so Explorer can unpack it without extra tools.

PyInstaller cannot cross-compile: ``--target`` must name the host platform.  It
exists mainly so CI jobs can assert which platform they expect to package.
Note that only the Windows backend is implemented today; bundles built on
macOS/Linux contain the shared layers and produce a warning that the native
tray/notification backend is still missing.
"""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Distribution folder/app name.
APP_NAME = "BarkDesktopClient"
# Executable name inside the onedir bundle (PyInstaller names the staging
# folder after it, so the folder is renamed to APP_NAME afterwards).
EXE_NAME = "bark"
ENTRY = Path("bark_pc/launcher.py")
ROOT = Path(__file__).resolve().parent.parent.parent
ICON_DIR = ROOT / "bark_pc" / "assets" / "icons"

# Modules every platform needs: the Tk UI, the Tk/Pillow glue and the anyio
# backend that the push receiver imports dynamically.
COMMON_HIDDEN_IMPORTS = (
    "tkinter",
    "tkinter.ttk",
    "tkinter.font",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "PIL._tkinter_finder",
    "anyio._backends._asyncio",
    "bumble.device",
    "bumble.keys",
    "bumble.pairing",
    "bumble.profiles.ancs",
    "bumble.transport.usb",
)

COMMON_COLLECT_ALL = (
    "pystray",
    "pypush",
    "segno",
    "websockets",
    # Bumble reaches the USB Bluetooth adapter through libusb; this package
    # ships the libusb DLL that `bumble.transport.usb` loads at runtime.
    "libusb_package",
)

# WinRT namespace packages and the pystray Win32 backend are imported by name
# (see docs/validation/spike_T2.md), so they must be listed explicitly.
WINDOWS_HIDDEN_IMPORTS = (
    "winrt.windows.ui.notifications",
    "winrt.windows.media.audio",
    "winrt.windows.devices.enumeration",
    "winrt.windows.storage.streams",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.windows.data.xml.dom",
    "pystray._win32",
)
WINDOWS_COLLECT_ALL = ("windows_toasts", "winrt")

# PyInstaller writes macOS icons as .icns and the app bundle wants a
# human-readable executable name.
ICNS_SIZES = (
    (16, 16),
    (32, 32),
    (64, 64),
    (128, 128),
    (256, 256),
    (512, 512),
    (1024, 1024),
)


@dataclass(frozen=True)
class BuildTarget:
    """Everything that differs between desktop platforms."""

    key: str
    label: str
    pyinstaller_name: str
    windowed: bool
    icon_name: str | None
    archive_format: str
    bundle_extension: str
    hidden_imports: tuple[str, ...]
    collect_all: tuple[str, ...]

    @property
    def bundle_name(self) -> str:
        return f"{APP_NAME}{self.bundle_extension}"

    @property
    def is_app_bundle(self) -> bool:
        """True for macOS ``.app`` directories (docs live in Contents/Resources)."""
        return self.bundle_extension == ".app"


WINDOWS = BuildTarget(
    key="windows",
    label="Windows",
    pyinstaller_name=EXE_NAME,
    windowed=True,
    icon_name="app.ico",
    archive_format="zip",
    bundle_extension="",
    hidden_imports=COMMON_HIDDEN_IMPORTS + WINDOWS_HIDDEN_IMPORTS,
    collect_all=COMMON_COLLECT_ALL + WINDOWS_COLLECT_ALL,
)

MACOS = BuildTarget(
    key="macos",
    label="macOS",
    # A windowed macOS build is an .app bundle; name it after the product so
    # Finder and the menu bar show the right title.
    pyinstaller_name=APP_NAME,
    windowed=True,
    icon_name="app.icns",
    archive_format="tar.gz",
    bundle_extension=".app",
    hidden_imports=COMMON_HIDDEN_IMPORTS,
    collect_all=COMMON_COLLECT_ALL,
)

LINUX = BuildTarget(
    key="linux",
    label="Linux",
    pyinstaller_name=EXE_NAME,
    # PyInstaller ignores --windowed on Linux; the executable is always a
    # console binary and the tray comes from pystray.
    windowed=False,
    icon_name=None,
    archive_format="tar.gz",
    bundle_extension="",
    hidden_imports=COMMON_HIDDEN_IMPORTS,
    collect_all=COMMON_COLLECT_ALL,
)

TARGETS = {target.key: target for target in (WINDOWS, MACOS, LINUX)}
TARGET_CHOICES = ("auto", *TARGETS)
ARCHIVE_CHOICES = ("auto", "zip", "tar.gz", "none")

_ARCH_ALIASES = {
    "amd64": "x64",
    "x86_64": "x64",
    "x64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "i386": "x86",
    "i686": "x86",
    "x86": "x86",
}


def detect_target(system: str | None = None) -> BuildTarget:
    """Pick the build target for *system* (defaults to ``sys.platform``)."""
    name = (system or sys.platform).lower()
    if name.startswith("win") or name in {"cygwin", "msys"}:
        return WINDOWS
    if name == "darwin":
        return MACOS
    if name.startswith("linux"):
        return LINUX
    supported = ", ".join(target.key for target in TARGETS.values())
    raise SystemExit(f"unsupported build host {name!r}; supported: {supported}")


def resolve_target(value: str = "auto", *, system: str | None = None) -> BuildTarget:
    """Resolve ``--target`` and refuse impossible cross-compilation."""
    host = detect_target(system)
    if value in ("", "auto"):
        return host
    target = TARGETS.get(value)
    if target is None:
        supported = ", ".join(TARGET_CHOICES)
        raise SystemExit(f"unknown target {value!r}; choose one of: {supported}")
    if target.key != host.key:
        raise SystemExit(
            "PyInstaller cannot cross-compile: "
            f"host is {host.label}, requested target is {target.label}"
        )
    return target


def host_architecture(machine: str | None = None) -> str:
    """Normalise ``platform.machine()`` to x64/arm64/x86 (or the raw value)."""
    if machine is None:
        machine = platform.machine()
    name = machine.strip().lower()
    return _ARCH_ALIASES.get(name, name.replace(" ", "-") or "unknown")


def archive_format(target: BuildTarget, value: str = "auto") -> str | None:
    """Return ``zip``/``tar.gz``/``None`` for the requested archive setting."""
    if value in ("", "auto"):
        return target.archive_format
    if value not in ("zip", "tar.gz", "none"):
        raise SystemExit(f"unknown archive format {value!r}; choose one of: {ARCHIVE_CHOICES}")
    return None if value == "none" else value


def archive_name(target: BuildTarget, fmt: str, machine: str | None = None) -> str:
    """Platform-tagged archive name, e.g. ``BarkDesktopClient-linux-x64.tar.gz``."""
    return f"{APP_NAME}-{target.key}-{host_architecture(machine)}.{fmt}"


def build_command(target: BuildTarget, dist_dir: Path, build_dir: Path) -> list[str]:
    """The PyInstaller argument list for *target* (separated for testability)."""
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        target.pyinstaller_name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(build_dir),
        "--specpath",
        str(build_dir),
    ]
    if target.windowed:
        command.append("--windowed")
    for module in target.hidden_imports:
        command += ["--hidden-import", module]
    for package in target.collect_all:
        command += ["--collect-all", package]
    icon = find_icon(target, build_dir)
    if icon is not None:
        command += ["--icon", str(icon)]
    command.append(str(ROOT / ENTRY))
    return command


def find_icon(target: BuildTarget, build_dir: Path | None = None) -> Path | None:
    """Locate the launcher icon, generating the macOS ``.icns`` if needed.

    The generated ``.icns`` goes into the build directory, never into the
    checked-in assets, so a build leaves the source tree clean.
    """
    if target.icon_name is None:
        return None
    icon = ICON_DIR / target.icon_name
    if icon.exists():
        return icon
    if icon.suffix == ".icns":
        generated = (build_dir or ICON_DIR) / icon.name
        if ensure_icns(ICON_DIR / "app.png", generated):
            return generated
    # PyInstaller accepts a single PNG as a fallback, but scales it badly.
    fallback = icon.with_suffix(".png")
    return fallback if fallback.exists() else None


def ensure_icns(source: Path, target: Path) -> bool:
    """Convert the 64 px PNG artwork into an ``.icns`` with Pillow.

    Pillow renders the missing sizes itself, so no macOS-only ``iconutil`` run
    is required and the build works on any host.  Returns False when Pillow or
    the source image is unavailable; the build then proceeds without an icon.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a runtime dependency
        return False
    if not source.exists():
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as image:
            image.convert("RGBA").save(target, format="ICNS", sizes=list(ICNS_SIZES))
    except Exception as exc:  # pragma: no cover - Pillow format problem
        print(f"warning: could not create {target.name}: {exc}")
        return False
    return True


def _rename_with_retry(
    source: Path, target: Path, attempts: int = 10, delay: float = 0.5
) -> None:
    """Rename a freshly written bundle folder.

    Windows sometimes reports a transient ``PermissionError`` while a virus
    scanner or the search indexer still holds handles on the new files, so retry
    briefly before giving up.
    """
    for attempt in range(attempts):
        try:
            source.rename(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _reset_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _stage_bundle(target: BuildTarget, dist_dir: Path) -> Path:
    """Move PyInstaller's output to the stable ``BarkDesktopClient`` name."""
    produced = dist_dir / target.pyinstaller_name
    if target.is_app_bundle:
        app = produced.with_name(produced.name + ".app")
        if not app.exists():
            raise SystemExit(
                f"PyInstaller did not create {app}; a windowed macOS build needs "
                "the .app bundle output"
            )
        bundle = dist_dir / target.bundle_name
        if bundle != app:
            _reset_path(bundle)
            _rename_with_retry(app, bundle)
        # A console-style folder may also be emitted next to the .app; the
        # .app is the distribution, so drop the duplicate.
        _reset_path(produced)
        return bundle

    if not produced.exists():
        raise SystemExit(f"PyInstaller did not create {produced}")
    bundle = dist_dir / target.bundle_name
    if bundle != produced:
        _reset_path(bundle)
        _rename_with_retry(produced, bundle)
    return bundle


def bundle_docs_dir(bundle: Path) -> Path:
    """Where NOTICE/README/quickstart go inside the bundled product."""
    if bundle.suffix == ".app":
        return bundle / "Contents" / "Resources"
    return bundle


def copy_docs(bundle: Path) -> None:
    extras = bundle_docs_dir(bundle)
    extras.mkdir(parents=True, exist_ok=True)
    for extra in ("NOTICE", "README.md", "LICENSE"):
        source = ROOT / extra
        if source.exists():
            shutil.copy2(source, extras / extra)
    quickstart = ROOT / "specs" / "001-bark-desktop-client" / "quickstart.md"
    if quickstart.exists():
        docs = extras / "docs"
        docs.mkdir(exist_ok=True)
        shutil.copy2(quickstart, docs / "quickstart.md")


def _write_zip(bundle: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        for path in sorted(bundle.rglob("*")):
            arcname = path.relative_to(bundle.parent).as_posix()
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            if path.is_dir():
                handle.writestr(info, b"")
            else:
                with path.open("rb") as source, handle.open(info, "w") as target:
                    shutil.copyfileobj(source, target)


def _write_tar(bundle: Path, archive: Path) -> None:
    # tar keeps the executable bit and any symlinks PyInstaller created inside
    # the bundle, which zip does not do reliably.
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(bundle, arcname=bundle.name, recursive=True)


def write_archive(bundle: Path, archive: Path, fmt: str) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    if fmt == "zip":
        _write_zip(bundle, archive)
    elif fmt == "tar.gz":
        _write_tar(bundle, archive)
    else:  # pragma: no cover - guarded by archive_format()
        raise SystemExit(f"unsupported archive format {fmt!r}")


def backend_available(target: BuildTarget) -> bool:
    """Whether ``bark_pc/platform/<os>/`` exists for this target."""
    return (ROOT / "bark_pc" / "platform" / target.key).is_dir()


def build(
    target: BuildTarget,
    *,
    archive: str = "auto",
    dist_dir: Path | None = None,
    build_dir: Path | None = None,
) -> Path:
    """Run PyInstaller and package the result. Returns the bundle path."""
    dist_dir = dist_dir or ROOT / "dist"
    build_dir = build_dir or ROOT / "build"
    fmt = archive_format(target, archive)

    if target.key != WINDOWS.key and not backend_available(target):
        print(
            f"warning: bark_pc/platform/{target.key} is not implemented; "
            f"the {target.label} bundle contains the shared layers only and "
            "will not start until the native backend lands"
        )

    command = build_command(target, dist_dir, build_dir)
    print("running:", " ".join(command))
    subprocess.run(command, check=True, cwd=ROOT)

    bundle = _stage_bundle(target, dist_dir)
    copy_docs(bundle)
    if fmt is not None:
        name = archive_name(target, fmt)
        archive_path = dist_dir / name
        write_archive(bundle, archive_path, fmt)
        print(f"wrote {archive_path}")
    print(f"portable build at {bundle}")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the portable desktop bundle for this platform."
    )
    parser.add_argument(
        "--target",
        default="auto",
        choices=TARGET_CHOICES,
        help="platform to package (default: detect the host; no cross-compiling)",
    )
    parser.add_argument(
        "--archive",
        default="auto",
        choices=ARCHIVE_CHOICES,
        help="archive format (default: zip on Windows, tar.gz elsewhere)",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        help="deprecated alias for --archive none (keep only the folder)",
    )
    args = parser.parse_args(argv)
    target = resolve_target(args.target)
    archive = "none" if args.no_zip else args.archive
    build(target, archive=archive)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
