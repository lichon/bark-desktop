"""Find the web links inside a notification body.

Bark bodies are plain text written by arbitrary senders, so a link is often
glued to a Chinese sentence with no whitespace around it::

    请看 https://example.com/a?b=1。（详情）

The matcher therefore stops at whitespace and at full-width punctuation, and it
drops the punctuation an English sentence leaves behind (``see
https://example.com.``).  Only web addresses are recognised: a body is shown
verbatim, so guessing that ``foo.py`` or ``1.5`` is a host would create links
where none were meant.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# Full-width punctuation and CJK letters end a bare address: a Chinese sentence
# can run straight into the link with no space between them.
_CJK = (
    r"\u2e80-\u2fff\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef"
)
URL_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/-])"
    r"(?:https?://|www\.)"
    rf"[^\s<>\"'`{_CJK}]+"
)

# Punctuation a sentence adds after the address itself.
_TRAILING = ".,;:!?'\"*"
# Closing characters are kept while they still balance an opener inside the URL
# (`https://en.wikipedia.org/wiki/Foo_(bar)`).
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def _trim(url: str) -> str:
    """Drop the sentence punctuation that ended up on the end of *url*."""
    while url:
        last = url[-1]
        if last in _TRAILING:
            url = url[:-1]
            continue
        opener = _CLOSERS.get(last)
        if opener is not None and url.count(opener) < url.count(last):
            url = url[:-1]
            continue
        break
    return url


def _is_web_url(url: str) -> bool:
    return open_target(url) is not None


def iter_urls(text: str | None):
    """Yield ``(start, end, url)`` for every web address in *text*."""
    if not text:
        return
    for match in URL_PATTERN.finditer(text):
        url = _trim(match.group(0))
        if not _is_web_url(url):
            continue
        yield match.start(), match.start() + len(url), url


def open_target(url: str | None) -> str | None:
    """The address to hand to the browser, or None when it is not web-safe.

    A bare `www.` match gets its scheme here so the rest of the app only ever
    deals with `http(s)://` addresses.
    """
    candidate = (url or "").strip()
    if not candidate:
        return None
    if candidate.lower().startswith("www."):
        candidate = f"https://{candidate}"
    if not candidate.lower().startswith(("http://", "https://")):
        return None
    try:
        parts = urlsplit(candidate)
    except ValueError:  # pragma: no cover - defensive
        return None
    return candidate if parts.netloc else None
