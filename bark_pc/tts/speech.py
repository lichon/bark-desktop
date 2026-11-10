"""Turn a stored `Notification` into one sentence a voice can read out.

Bark bodies are frequently markdown (`![chart](https://…)`, links, bare URLs).
Those are pleasant in a toast and unbearable read aloud, so they are reduced to
their text before speaking.  Pure functions only — the rules are pinned by
`tests/unit/test_tts.py` and are provider independent.
"""

from __future__ import annotations

import re

HIDDEN_PREVIEW_SPEECH = "New notification"
DEFAULT_TTS_MAX_CHARS = 300

_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_TRAILING_PUNCTUATION = " \t.,;:!?，。；：！？…"


def speech_text(
    notification,
    *,
    include_body: bool = True,
    hide_preview: bool = False,
    max_chars: int = DEFAULT_TTS_MAX_CHARS,
) -> str:
    """The line to speak for *notification*, or "" when there is nothing useful."""
    if hide_preview:
        return HIDDEN_PREVIEW_SPEECH
    title = _plain(getattr(notification, "display_title", "") or "").rstrip(_TRAILING_PUNCTUATION)
    body = _plain(getattr(notification, "body", "") or "") if include_body else ""
    parts = [part for part in (title, body) if part]
    if not parts:
        return ""
    return _clip(". ".join(parts), max_chars)


def _plain(text: str) -> str:
    """Strip markdown/URLs and flatten the text onto a single line."""
    text = _MARKDOWN_IMAGE.sub("", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _BARE_URL.sub("", text)
    return " ".join(text.split())


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip(_TRAILING_PUNCTUATION) + "…"
