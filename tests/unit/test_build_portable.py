"""Unit tests for the cross-platform portable build script.

These tests never run PyInstaller; they cover target detection, the generated
command line, archive naming and the packaging helpers.
"""

from __future__ import annotations

import tarfile
import zipfile
from pathlib import Path

import pytest

from bark_pc.tools import build_portable as bp


def test_detect_target_maps_host_platforms():
    assert bp.detect_target("win32") is bp.WINDOWS
    assert bp.detect_target("cygwin") is bp.WINDOWS
    assert bp.detect_target("darwin") is bp.MACOS
    assert bp.detect_target("linux") is bp.LINUX
    assert bp.detect_target("linux2") is bp.LINUX


def test_detect_target_rejects_unknown_platforms():
    with pytest.raises(SystemExit, match="unsupported build host"):
        bp.detect_target("aix")


def test_resolve_target_defaults_to_the_host():
    assert bp.resolve_target("auto", system="darwin") is bp.MACOS
    assert bp.resolve_target("", system="linux") is bp.LINUX


def test_resolve_target_refuses_cross_compilation():
    with pytest.raises(SystemExit, match="cannot cross-compile"):
        bp.resolve_target("windows", system="linux")


def test_resolve_target_rejects_unknown_names():
    with pytest.raises(SystemExit, match="unknown target"):
        bp.resolve_target("beos", system="linux")


def test_windows_keeps_the_winrt_hidden_imports():
    imports = bp.WINDOWS.hidden_imports
    assert "winrt.windows.ui.notifications" in imports
    assert "pystray._win32" in imports
    assert "windows_toasts" in bp.WINDOWS.collect_all


@pytest.mark.parametrize("target", [bp.MACOS, bp.LINUX])
def test_non_windows_targets_omit_winrt(target):
    assert not any(
        module.startswith(("winrt", "windows_toasts")) for module in target.hidden_imports
    )
    assert "winrt" not in target.collect_all
    assert "windows_toasts" not in target.collect_all


def test_the_bluetooth_stack_is_bundled_on_every_platform():
    for target in (bp.WINDOWS, bp.MACOS, bp.LINUX):
        assert "libusb_package" in target.collect_all
        assert "bumble.transport.usb" in target.hidden_imports
        assert "bumble.profiles.ancs" in target.hidden_imports


def test_build_command_selects_windowed_mode_and_icons(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "find_icon", lambda target, build_dir=None: None)
    command = bp.build_command(bp.WINDOWS, tmp_path / "dist", tmp_path / "build")
    assert command[command.index("--name") + 1] == bp.EXE_NAME
    assert "--windowed" in command
    assert "pystray._win32" in command
    assert "--collect-all" in command
    assert command[-1].endswith("launcher.py")


def test_linux_build_command_is_console_only(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "find_icon", lambda target, build_dir=None: None)
    command = bp.build_command(bp.LINUX, tmp_path / "dist", tmp_path / "build")
    assert "--windowed" not in command
    assert "--icon" not in command
    assert not any("winrt" in part for part in command)


def test_macos_build_command_names_the_app_bundle(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "find_icon", lambda target, build_dir=None: None)
    command = bp.build_command(bp.MACOS, tmp_path / "dist", tmp_path / "build")
    assert command[command.index("--name") + 1] == bp.APP_NAME
    assert "--windowed" in command
    assert not any("winrt" in part for part in command)


def test_find_icon_generates_icns_when_missing(monkeypatch, tmp_path):
    pytest.importorskip("PIL.Image")
    source = tmp_path / "app.png"
    from PIL import Image

    Image.new("RGBA", (64, 64), (37, 99, 235, 255)).save(source, format="PNG")
    monkeypatch.setattr(bp, "ICON_DIR", tmp_path)
    target = bp.MACOS
    icon = bp.find_icon(target, tmp_path / "build")
    assert icon is not None
    assert icon.suffix == ".icns"
    assert icon.exists()
    assert icon.parent == tmp_path / "build"


def test_frozen_import_smoke_test_follows_the_target():
    from bark_pc.tools.spike_frozen_imports import modules_for

    windows = modules_for(bp.WINDOWS)
    linux = modules_for(bp.LINUX)
    assert "winrt.windows.foundation" in windows
    assert "windows_toasts" in windows
    assert "winrt.windows.foundation" not in linux
    assert "pypush.apns" in linux
    assert "bumble.transport.usb" in windows
    assert "bumble.transport.usb" in linux


def test_archive_format_defaults_per_platform():
    assert bp.archive_format(bp.WINDOWS) == "zip"
    assert bp.archive_format(bp.MACOS) == "tar.gz"
    assert bp.archive_format(bp.LINUX) == "tar.gz"
    assert bp.archive_format(bp.WINDOWS, "none") is None
    assert bp.archive_format(bp.LINUX, "zip") == "zip"


def test_archive_format_rejects_unknown_values():
    with pytest.raises(SystemExit, match="unknown archive format"):
        bp.archive_format(bp.WINDOWS, "rar")


def test_archive_name_is_platform_and_arch_tagged():
    assert bp.archive_name(bp.WINDOWS, "zip", "AMD64") == "BarkDesktopClient-windows-x64.zip"
    assert (
        bp.archive_name(bp.MACOS, "tar.gz", "arm64")
        == "BarkDesktopClient-macos-arm64.tar.gz"
    )
    assert bp.archive_name(bp.LINUX, "tar.gz", "aarch64") == "BarkDesktopClient-linux-arm64.tar.gz"


def test_host_architecture_falls_back_to_the_raw_value():
    assert bp.host_architecture("riscv64") == "riscv64"
    assert bp.host_architecture("") == "unknown"


def test_bundle_docs_dir_is_inside_app_bundles(tmp_path):
    assert bp.bundle_docs_dir(tmp_path / "BarkDesktopClient") == tmp_path / "BarkDesktopClient"
    app = tmp_path / "BarkDesktopClient.app"
    assert bp.bundle_docs_dir(app) == app / "Contents" / "Resources"


def test_stage_bundle_renames_the_onedir_output(tmp_path):
    produced = tmp_path / bp.EXE_NAME
    produced.mkdir()
    (produced / "bark").write_text("binary", encoding="utf-8")
    bundle = bp._stage_bundle(bp.LINUX, tmp_path)
    assert bundle == tmp_path / bp.APP_NAME
    assert (bundle / "bark").is_file()
    assert not produced.exists()


def test_stage_bundle_uses_the_macos_app_and_drops_the_folder(tmp_path):
    app = tmp_path / (bp.APP_NAME + ".app")
    app.mkdir()
    (app / "Contents").mkdir()
    folder = tmp_path / bp.APP_NAME
    folder.mkdir()
    bundle = bp._stage_bundle(bp.MACOS, tmp_path)
    assert bundle == app
    assert app.is_dir()
    assert not folder.exists()


def test_stage_bundle_fails_when_pyinstaller_produced_nothing(tmp_path):
    with pytest.raises(SystemExit, match="did not create"):
        bp._stage_bundle(bp.WINDOWS, tmp_path)


def _sample_bundle(root: Path) -> Path:
    bundle = root / bp.APP_NAME
    (bundle / "docs").mkdir(parents=True)
    (bundle / bp.EXE_NAME).write_text("binary", encoding="utf-8")
    (bundle / "docs" / "quickstart.md").write_text("hello", encoding="utf-8")
    return bundle


def test_write_zip_archives_the_bundle_folder(tmp_path):
    bundle = _sample_bundle(tmp_path / "dist")
    archive = tmp_path / "out.zip"
    bp.write_archive(bundle, archive, "zip")
    with zipfile.ZipFile(archive) as handle:
        names = set(handle.namelist())
    assert f"{bp.APP_NAME}/{bp.EXE_NAME}" in names
    assert f"{bp.APP_NAME}/docs/quickstart.md" in names


def test_write_tar_keeps_the_bundle_folder(tmp_path):
    bundle = _sample_bundle(tmp_path / "dist")
    archive = tmp_path / "out.tar.gz"
    bp.write_archive(bundle, archive, "tar.gz")
    with tarfile.open(archive) as handle:
        names = set(handle.getnames())
    assert bp.APP_NAME in names
    assert f"{bp.APP_NAME}/{bp.EXE_NAME}" in names


def test_copy_docs_places_readme_inside_app_bundles(tmp_path):
    app = tmp_path / (bp.APP_NAME + ".app")
    app.mkdir()
    bp.copy_docs(app)
    resources = app / "Contents" / "Resources"
    assert resources.is_dir()
    assert (resources / "NOTICE").is_file()
