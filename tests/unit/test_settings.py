"""Settings defaults, validation fallback, atomic writes."""

from __future__ import annotations

import json
from pathlib import Path

from bark_pc.core.config import (
    Settings,
    SettingsStore,
    validate_server_url,
    validate_supabase_url,
)


def test_defaults_match_the_data_model(settings_store):
    settings = settings_store.load()
    assert settings.server_url == "https://api.day.app"
    assert settings.push_topic == "me.fin.bark"
    assert settings.history_retention_count == 500
    assert settings.history_retention_days == 30
    assert settings.image_cache_mb == 200
    assert settings.toasts_enabled is True
    assert settings.hide_preview_text is False
    assert settings.silence_until is None
    assert settings.autostart_enabled is False
    assert settings.log_level == "info"


def test_missing_file_yields_defaults(tmp_path):
    from bark_pc.core.config import Paths

    paths = Paths(app_dir=tmp_path)
    store = SettingsStore(paths)
    settings = store.load()
    assert settings.history_retention_count == 500


def test_out_of_range_values_fall_back_to_defaults(portable_paths):
    portable_paths.settings_path.write_text(
        json.dumps(
            {
                "history_retention_count": 5,
                "history_retention_days": 999,
                "image_cache_mb": "lots",
                "toasts_enabled": "yes",
                "log_level": "verbose",
                "server_url": "ftp://example.com",
                "push_topic": "",
            }
        ),
        encoding="utf-8",
    )
    store = SettingsStore(portable_paths)
    settings = store.load()
    assert settings.history_retention_count == 500
    assert settings.history_retention_days == 30
    assert settings.image_cache_mb == 200
    assert settings.toasts_enabled is True
    assert settings.log_level == "info"
    assert settings.server_url == "https://api.day.app"
    assert settings.push_topic == "me.fin.bark"
    assert len(settings.warnings) >= 7


def test_in_range_values_are_kept(portable_paths):
    portable_paths.settings_path.write_text(
        json.dumps(
            {
                "history_retention_count": 1_000,
                "history_retention_days": 7,
                "image_cache_mb": 512,
                "hide_preview_text": True,
                "silence_until": 1_762_732_800_000,
                "server_url": "http://127.0.0.1:8080/",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.history_retention_count == 1_000
    assert settings.history_retention_days == 7
    assert settings.image_cache_mb == 512
    assert settings.hide_preview_text is True
    assert settings.silence_until == 1_762_732_800_000
    assert settings.server_url == "http://127.0.0.1:8080"


def test_atomic_write_leaves_no_temp_files(portable_paths):
    store = SettingsStore(portable_paths)
    store.save(Settings(history_retention_count=600))
    store.save(Settings(history_retention_count=700))

    leftovers = [
        path
        for path in portable_paths.data_dir.iterdir()
        if path.name.startswith(".settings.json") or path.name.endswith(".tmp")
    ]
    assert leftovers == []
    assert (
        json.loads(portable_paths.settings_path.read_text(encoding="utf-8"))[
            "history_retention_count"
        ]
        == 700
    )


def test_corrupt_settings_file_falls_back_to_defaults(portable_paths):
    portable_paths.settings_path.write_text("{not json", encoding="utf-8")
    settings = SettingsStore(portable_paths).load()
    assert settings.history_retention_count == 500


def test_update_persists_changes(portable_paths):
    store = SettingsStore(portable_paths)
    store.update(server_url="http://127.0.0.1:8080", autostart_enabled=True)
    reloaded = SettingsStore(portable_paths).load()
    assert reloaded.server_url == "http://127.0.0.1:8080"
    assert reloaded.autostart_enabled is True


def test_set_last_connected_uses_a_timestamp(portable_paths):
    store = SettingsStore(portable_paths)
    store.set_last_connected(1_700_000_000_000)
    assert SettingsStore(portable_paths).load().last_connected_at == 1_700_000_000_000


def test_server_url_normalization():
    assert validate_server_url("example.com") == "https://example.com"
    assert validate_server_url("https://Example.com/") == "https://example.com"
    assert validate_server_url("http://127.0.0.1:8080/prefix/") == "http://127.0.0.1:8080/prefix"
    for bad in ("", "ftp://example.com", "https://user:pa@example.com", "https://example.com?x=1"):
        try:
            validate_server_url(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected {bad!r} to be rejected")


def test_supabase_realtime_is_optional_and_off_by_default():
    settings = Settings()
    assert settings.supabase_enabled is False
    assert settings.supabase_url == ""
    assert settings.supabase_topic == "bark"


def test_legacy_sb_server_moves_to_the_optional_room(portable_paths):
    portable_paths.settings_path.write_text(
        json.dumps(
            {
                "server_url": "sb://demo-project.supabase.co",
                "supabase_key": "sb_publishable_example",
                "push_topic": "alerts",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.server_url == "https://api.day.app"
    assert settings.push_topic == "me.fin.bark"
    assert settings.supabase_enabled is True
    assert settings.supabase_url == "sb://demo-project.supabase.co"
    assert settings.supabase_key == "sb_publishable_example"
    assert settings.supabase_topic == "alerts"


def test_supabase_project_addresses_are_normalized():
    assert validate_supabase_url("") == ""
    assert validate_supabase_url("sb://demo-project.supabase.co") == (
        "sb://demo-project.supabase.co"
    )
    assert validate_supabase_url("demo-project.supabase.co") == (
        "sb://demo-project.supabase.co"
    )
    assert validate_supabase_url("https://demo-project.supabase.co/") == (
        "sb://demo-project.supabase.co"
    )


def test_portable_paths_live_under_the_app_folder(portable_paths):
    assert portable_paths.db_path.parent == portable_paths.data_dir
    assert portable_paths.images_dir.parent == portable_paths.data_dir
    assert portable_paths.logs_dir.parent == portable_paths.data_dir
    assert isinstance(Path(portable_paths.identity_path), Path)


def test_message_route_defaults_to_the_realtime_room(settings_store):
    settings = settings_store.load()
    assert settings.message_route == "realtime"
    assert settings.message_route_url == ""


def test_message_route_and_bark_url_are_validated(portable_paths):
    portable_paths.settings_path.write_text(
        json.dumps(
            {
                "message_route": "BARK",
                "message_route_url": "api.day.app/deviceKEY/?group=ops",
            }
        ),
        encoding="utf-8",
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.message_route == "bark"
    assert settings.message_route_url == "https://api.day.app/deviceKEY?group=ops"


def test_invalid_message_route_and_bark_url_fall_back(portable_paths):
    portable_paths.settings_path.write_text(
        json.dumps({"message_route": "telegram", "message_route_url": "ftp://host/key"}),
        encoding="utf-8",
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.message_route == "realtime"
    assert settings.message_route_url == ""
    assert len(settings.warnings) >= 2
