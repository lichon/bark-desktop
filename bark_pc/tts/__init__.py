"""Text-to-speech: provider registry, factory and the notification→speech helper.

The default provider is `windows` (SAPI 5 through COM, no extra dependency).
Adding a provider means registering one engine factory here; nothing else in the
app knows which voice produced a sentence.

    from bark_pc.tts import build_tts_provider, speech_text
    provider = build_tts_provider("windows", rate=0, volume=100)
    provider.speak(speech_text(notification))
"""

from __future__ import annotations

import logging
import sys

from .base import (
    DEFAULT_TTS_PROVIDER,
    TTS_PROVIDERS,
    NullTtsProvider,
    TtsProvider,
    should_speak,
)
from .speech import DEFAULT_TTS_MAX_CHARS, HIDDEN_PREVIEW_SPEECH, speech_text
from .worker import ThreadedTtsProvider

logger = logging.getLogger("bark_pc.tts")

PROVIDER_LABELS = {
    "windows": "Windows system voice",
    "none": "Off (no voice)",
}
PROVIDER_LABEL_TO_NAME = {label: name for name, label in PROVIDER_LABELS.items()}

TTS_PREVIEW_TEXT = "Bark desktop client. Notifications will be read aloud like this."


def _windows_engine_factory(**kwargs):
    from ..platform.windows.sapi import SapiEngine  # lazy: COM only on first use

    return SapiEngine(**kwargs)


_ENGINE_FACTORIES = {"windows": _windows_engine_factory}


def normalize_provider(name: str | None) -> str:
    provider = (name or "").strip().lower()
    return provider if provider in TTS_PROVIDERS else DEFAULT_TTS_PROVIDER


def build_tts_provider(
    name: str = DEFAULT_TTS_PROVIDER,
    *,
    rate: int = 0,
    volume: int = 100,
    log: logging.Logger | None = None,
) -> TtsProvider:
    """Return a provider for *name*; never raises, degrades to `NullTtsProvider`."""
    log = log or logger
    supplied = (name or "").strip().lower()
    provider = normalize_provider(supplied)
    if supplied and supplied != provider:
        log.warning("unknown text-to-speech provider %r; using %s", name, provider)
    factory = _ENGINE_FACTORIES.get(provider)
    if factory is None:
        return NullTtsProvider()
    if not sys.platform.startswith("win"):
        log.warning("text-to-speech provider %s is not available on this platform", provider)
        return NullTtsProvider()
    return ThreadedTtsProvider(factory, name=provider, rate=rate, volume=volume, log=log)


__all__ = [
    "DEFAULT_TTS_MAX_CHARS",
    "DEFAULT_TTS_PROVIDER",
    "HIDDEN_PREVIEW_SPEECH",
    "NullTtsProvider",
    "PROVIDER_LABELS",
    "PROVIDER_LABEL_TO_NAME",
    "TTS_PROVIDERS",
    "TTS_PREVIEW_TEXT",
    "TtsProvider",
    "ThreadedTtsProvider",
    "build_tts_provider",
    "normalize_provider",
    "should_speak",
    "speech_text",
]
