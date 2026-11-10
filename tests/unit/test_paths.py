"""Data lives in the per-user AppData folder, with a one-time legacy move."""

from __future__ import annotations

import sys

from bark_pc.core import config
from bark_pc.core.config import Paths, migrate_legacy_data, resolve_paths


def test_default_data_dir_uses_localappdata(monkeypatch, tmp_path):
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert config.default_data_dir("Bark") == local / "Bark"


def test_default_data_dir_falls_back_to_appdata(monkeypatch, tmp_path):
    roaming = tmp_path / "AppData" / "Roaming"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setenv("APPDATA", str(roaming))
    assert config.default_data_dir("Bark") == roaming / "Bark"


def test_resolve_paths_puts_data_in_appdata(monkeypatch, tmp_path):
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv(config.APP_DIR_ENV, raising=False)
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)

    paths = resolve_paths(tmp_path / "app")
    assert paths.app_dir == (tmp_path / "app").resolve()
    assert paths.data_dir == local / config.APP_DATA_DIRNAME
    assert paths.db_path.parent == paths.data_dir


def test_resolve_paths_portable_override(monkeypatch, tmp_path):
    local = tmp_path / "AppData" / "Local"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv(config.DATA_DIR_ENV, raising=False)

    paths = resolve_paths(tmp_path / "app", portable=True)
    assert paths.data_dir == (tmp_path / "app").resolve() / "data"


def test_resolve_paths_data_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom"
    monkeypatch.setenv(config.DATA_DIR_ENV, str(custom))
    paths = resolve_paths(tmp_path / "app")
    assert paths.data_dir == custom.resolve()


def test_migrate_legacy_data_moves_the_folder(tmp_path):
    app = tmp_path / "BarkDesktopClient"
    legacy = app / "data"
    legacy.mkdir(parents=True)
    (legacy / "bark.db").write_bytes(b"db")
    (legacy / "settings.json").write_text("{}", encoding="utf-8")

    target = tmp_path / "AppData" / "BarkDesktopClient"
    paths = Paths(app_dir=app, data_root=target)

    assert migrate_legacy_data(paths) is True
    assert (target / "bark.db").read_bytes() == b"db"
    assert (target / "settings.json").exists()
    assert not legacy.exists()


def test_migrate_legacy_data_never_overwrites(tmp_path):
    app = tmp_path / "BarkDesktopClient"
    legacy = app / "data"
    legacy.mkdir(parents=True)
    (legacy / "bark.db").write_bytes(b"old")

    target = tmp_path / "AppData" / "BarkDesktopClient"
    target.mkdir(parents=True)
    (target / "bark.db").write_bytes(b"new")

    paths = Paths(app_dir=app, data_root=target)

    assert migrate_legacy_data(paths) is False
    assert (target / "bark.db").read_bytes() == b"new"
    assert (legacy / "bark.db").read_bytes() == b"old"


def test_migrate_legacy_data_is_a_noop_for_portable_layout(tmp_path):
    app = tmp_path / "BarkDesktopClient"
    (app / "data").mkdir(parents=True)
    assert migrate_legacy_data(Paths(app_dir=app)) is False
