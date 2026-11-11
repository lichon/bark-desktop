"""Provider-neutral text-to-speech contract and the always-safe fallbacks.

Speech is an optional presentation channel next to the toast: it must never
block ingest, never raise into the UI, and never be required for the app to
work.  Everything below is standard library only, so `bark_pc.core.config` can
validate provider names without importing a platform module.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

DEFAULT_TTS_PROVIDER = "windows"
TTS_PROVIDERS = ("windows", "none")


@runtime_checkable
class TtsProvider(Protocol):
    """Speak lines of plain text; every method is safe to call at any time."""

    name: str

    def speak(self, text: str) -> bool: ...

    def stop(self) -> None: ...

    def shutdown(self, timeout: float = 2.0) -> None: ...


def should_speak(
    *,
    enabled: bool,
    silenced: bool,
    only_when_unlocked: bool,
    locked: bool,
) -> bool:
    """The one place that decides whether a notification may be read aloud.

    Speaking is the only channel that can be overheard by whoever is in the
    room, so a locked screen (user away) suppresses it unless the user
    explicitly asked to be read to anyway.
    """
    if not enabled or silenced:
        return False
    return not (only_when_unlocked and locked)


class NullTtsProvider:
    """No audio device: records what would have been spoken (tests, headless)."""

    name = "none"

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.stopped = 0

    def speak(self, text: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        self.spoken.append(text)
        return True

    def stop(self) -> None:
        self.stopped += 1

    def available(self, timeout: float = 0.0) -> bool:
        return False

    def configure(self, *, rate=None, volume=None) -> None:
        return None

    def reset(self) -> None:
        return None

    def shutdown(self, timeout: float = 2.0) -> None:
        return None
