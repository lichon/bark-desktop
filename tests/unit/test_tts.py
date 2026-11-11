"""Text-to-speech: spoken text, provider selection, worker failures.

No test here produces audio: the Windows engine is replaced by a fake that only
records calls, and the real SAPI object is exercised by one smoke test that is
allowed to skip when a machine has no voice installed.
"""

from __future__ import annotations

import sys
import time

import pytest

from bark_pc.core.config import Settings
from bark_pc.core.models import Notification
from bark_pc.tts import (
    HIDDEN_PREVIEW_SPEECH,
    PROVIDER_LABEL_TO_NAME,
    PROVIDER_LABELS,
    TTS_PROVIDERS,
    NullTtsProvider,
    ThreadedTtsProvider,
    build_tts_provider,
    normalize_provider,
    should_speak,
    speech_text,
)
from bark_pc.tts.base import DEFAULT_TTS_PROVIDER


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# Notification → spoken sentence
# --------------------------------------------------------------------------- #


def test_speech_text_joins_title_and_body():
    notification = Notification(title="Deploy done", body="build 412 is live")

    assert speech_text(notification) == "Deploy done. build 412 is live"


def test_speech_text_skips_the_body_when_asked():
    notification = Notification(title="Deploy done", body="build 412 is live")

    assert speech_text(notification, include_body=False) == "Deploy done"


def test_speech_text_uses_the_title_or_subtitle_either_way():
    assert speech_text(Notification(subtitle="CI")) == "CI"
    assert speech_text(Notification(title="CI", subtitle="nightly")) == "CI — nightly"
    assert speech_text(Notification(body="only a body")) == "only a body"


def test_hidden_preview_never_reads_the_content_aloud():
    notification = Notification(title="Secret", body="hunter2")

    assert speech_text(notification, hide_preview=True) == HIDDEN_PREVIEW_SPEECH


def test_speech_text_flattens_markdown_and_drops_urls():
    notification = Notification(
        title="Release",
        body="![chart](https://example.com/c.png)\nsee [the notes](https://example.com/n)\n"
        "or https://example.com/raw",
    )

    spoken = speech_text(notification)

    assert spoken == "Release. see the notes or"


def test_speech_text_is_clipped_on_a_word_boundary():
    notification = Notification(title="T", body="word " * 200)

    spoken = speech_text(notification, max_chars=60)

    assert len(spoken) <= 60
    assert spoken.endswith("…")
    assert "…" not in spoken[:-1]


def test_speech_text_is_empty_when_there_is_nothing_to_say():
    assert speech_text(Notification()) == ""
    assert speech_text(Notification(title="   ", body="\n")) == ""


# --------------------------------------------------------------------------- #
# Settings defaults + validation
# --------------------------------------------------------------------------- #


def test_tts_defaults_use_the_windows_provider_and_stay_off():
    settings = Settings()

    assert settings.tts_enabled is False
    assert settings.tts_provider == DEFAULT_TTS_PROVIDER == "windows"
    assert settings.tts_include_body is True
    assert settings.tts_only_when_unlocked is True
    assert settings.tts_rate == 0
    assert settings.tts_volume == 100
    assert settings.tts_max_chars == 300


def test_tts_settings_survive_a_save_and_load_round_trip(settings_store):
    settings = Settings(
        tts_enabled=True,
        tts_provider="none",
        tts_include_body=False,
        tts_only_when_unlocked=False,
        tts_rate=-4,
        tts_volume=40,
    )
    settings_store.save(settings)

    loaded = settings_store.load()

    assert (loaded.tts_enabled, loaded.tts_provider) == (True, "none")
    assert (loaded.tts_include_body, loaded.tts_rate, loaded.tts_volume) == (False, -4, 40)
    assert loaded.tts_only_when_unlocked is False


def test_invalid_tts_values_fall_back_to_defaults():
    settings = Settings.from_dict(
        {
            "tts_enabled": "yes",
            "tts_provider": "acme-cloud",
            "tts_only_when_unlocked": "sure",
            "tts_rate": 99,
            "tts_volume": -5,
            "tts_max_chars": 5,
        }
    )

    assert settings.tts_enabled is False
    assert settings.tts_provider == "windows"
    assert settings.tts_only_when_unlocked is True
    assert settings.tts_rate == 0
    assert settings.tts_volume == 100
    assert settings.tts_max_chars == 300
    assert len(settings.warnings) == 6


# --------------------------------------------------------------------------- #
# Speaking gate: locked screen suppresses speech
# --------------------------------------------------------------------------- #


def test_speech_needs_tts_on_and_no_silence_window():
    assert should_speak(enabled=True, silenced=False, only_when_unlocked=True, locked=False) is True
    assert (
        should_speak(enabled=False, silenced=False, only_when_unlocked=True, locked=False) is False
    )
    assert should_speak(enabled=True, silenced=True, only_when_unlocked=True, locked=False) is False


def test_a_locked_screen_suppresses_speech_unless_the_user_opted_out():
    assert should_speak(enabled=True, silenced=False, only_when_unlocked=True, locked=True) is False
    assert should_speak(enabled=True, silenced=False, only_when_unlocked=False, locked=True) is True


def test_provider_names_are_normalised_case_insensitively():
    assert Settings.from_dict({"tts_provider": "  WiNdOwS "}).tts_provider == "windows"
    assert normalize_provider("NONE") == "none"
    assert normalize_provider("acme") == "windows"


# --------------------------------------------------------------------------- #
# Provider factory
# --------------------------------------------------------------------------- #


def test_every_provider_has_a_menu_label():
    assert set(PROVIDER_LABELS) == set(TTS_PROVIDERS)
    assert PROVIDER_LABEL_TO_NAME["Off (no voice)"] == "none"


def test_factory_builds_the_windows_provider_on_windows():
    provider = build_tts_provider("windows")
    try:
        if sys.platform.startswith("win"):
            assert isinstance(provider, ThreadedTtsProvider)
            assert provider.name == "windows"
        else:  # the provider is Windows-only; elsewhere it degrades to silence
            assert isinstance(provider, NullTtsProvider)
    finally:
        provider.shutdown()


def test_factory_returns_a_null_provider_for_none():
    provider = build_tts_provider("none")

    assert isinstance(provider, NullTtsProvider)


def test_factory_falls_back_to_the_default_provider_for_unknown_names():
    for name in ("acme", ""):
        provider = build_tts_provider(name)
        try:
            if sys.platform.startswith("win"):
                assert isinstance(provider, ThreadedTtsProvider)
                assert provider.name == "windows"
            else:
                assert isinstance(provider, NullTtsProvider)
        finally:
            provider.shutdown()


def test_null_provider_records_lines_without_a_device():
    provider = NullTtsProvider()

    assert provider.speak("hello") is True
    assert provider.speak("   ") is False
    provider.stop()

    assert provider.spoken == ["hello"]
    assert provider.stopped == 1


# --------------------------------------------------------------------------- #
# Threaded provider: the worker owns the engine
# --------------------------------------------------------------------------- #


class FakeEngine:
    def __init__(self, *, rate: int = 0, volume: int = 100):
        self.spoken: list[str] = []
        self.stops = 0
        self.closed = False
        self.rate = rate
        self.volume = volume

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1

    def set_rate(self, rate: int) -> None:
        self.rate = rate

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def close(self) -> None:
        self.closed = True


def test_worker_speaks_stops_and_closes_its_engine():
    engines: list[FakeEngine] = []

    def factory(**kwargs):
        engine = FakeEngine(**kwargs)
        engines.append(engine)
        return engine

    provider = ThreadedTtsProvider(factory, name="fake", rate=2, volume=50)
    try:
        assert provider.available() is True
        assert provider.speak("first line") is True
        assert wait_for(lambda: engines[0].spoken == ["first line"])

        provider.stop()
        assert wait_for(lambda: engines[0].stops == 1)

        provider.configure(rate=-3, volume=80)
        assert wait_for(lambda: (engines[0].rate, engines[0].volume) == (-3, 80))
    finally:
        provider.shutdown()

    engine = engines[0]
    assert engine.closed is True
    assert (engine.rate, engine.volume) == (-3, 80)


def test_a_burst_keeps_only_the_newest_lines():
    class SlowEngine(FakeEngine):
        def speak(self, text: str) -> None:
            time.sleep(0.02)
            super().speak(text)

    engines: list[FakeEngine] = []

    def factory(**kwargs):
        engine = SlowEngine(**kwargs)
        engines.append(engine)
        return engine

    provider = ThreadedTtsProvider(factory, name="fake")
    try:
        assert provider.available() is True
        for index in range(30):
            provider.speak(f"line {index}")
        assert wait_for(lambda: "line 29" in engines[0].spoken)
        assert len(engines[0].spoken) <= 8
    finally:
        provider.shutdown()


def test_a_missing_engine_disables_speech_instead_of_raising():
    def factory(**_kwargs):
        raise OSError("no voice installed")

    provider = ThreadedTtsProvider(factory, name="broken")
    try:
        assert provider.available() is False
        assert provider.speak("anything") is False
        assert "no voice installed" in (provider.last_error or "")
    finally:
        provider.shutdown()

    provider.reset()

    assert provider.last_error is None
    assert provider.speak("anything") is True
    provider.shutdown()


def test_an_engine_error_does_not_kill_the_worker():
    class FlakyEngine(FakeEngine):
        def speak(self, text: str) -> None:
            if text == "boom":
                raise RuntimeError("synthesis failed")
            super().speak(text)

    engines: list[FakeEngine] = []

    def factory(**kwargs):
        engine = FlakyEngine(**kwargs)
        engines.append(engine)
        return engine

    provider = ThreadedTtsProvider(factory, name="flaky")
    try:
        provider.speak("boom")
        provider.speak("still here")
        assert wait_for(lambda: engines[0].spoken == ["still here"])
        assert "synthesis failed" in (provider.last_error or "")
    finally:
        provider.shutdown()


# --------------------------------------------------------------------------- #
# Windows engine smoke test (no audio; skips when SAPI is unavailable)
# --------------------------------------------------------------------------- #


@pytest.mark.windows_only
def test_sapi_engine_opens_and_closes():
    from bark_pc.platform.windows.sapi import SapiEngine

    try:
        engine = SapiEngine(rate=0, volume=100)
    except OSError as exc:  # e.g. windows-only marker ignored on CI
        pytest.skip(f"SAPI voice unavailable: {exc}")
    try:
        engine.set_rate(0)
        engine.set_volume(50)
        engine.stop()
    finally:
        engine.close()


def test_variant_layout_matches_the_native_stride():
    import ctypes

    from bark_pc.platform.windows.sapi import VARIANT

    expected = 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16

    assert ctypes.sizeof(VARIANT) == expected
