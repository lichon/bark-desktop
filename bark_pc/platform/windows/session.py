"""Is the interactive session locked? (screen locked / secure desktop showing)

Speech is the one notification channel nobody can skim: a push read out loud on
a locked workstation is heard by the room instead of by the user.  `bark_pc.app`
therefore asks here before it speaks, so the answer must be cheap, thread safe
and never wrong in the noisy direction.

Two probes, both pure `ctypes` (no `pywin32`):

* `WTSQuerySessionInformationW(WTSSessionInfoEx)` — the kernel's own session
  flags, the same source the Power/session APIs use.
* `OpenInputDesktop` — fails while Winlogon owns the input desktop, used as a
  fallback when the WTS query is unavailable.

Both are advisory: an unexpected failure answers "not locked" so a detection
problem degrades to the old behaviour instead of muting every notification.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger("bark_pc.session")

WTS_CURRENT_SESSION = 0xFFFFFFFF
WTSSessionInfoEx = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1

DESKTOP_READOBJECTS = 0x0001

WINSTATIONNAME_LENGTH = 32
USERNAME_LENGTH = 20
DOMAIN_LENGTH = 17


class _WTSINFOEX_LEVEL1_W(ctypes.Structure):
    """`WTSINFOEX_LEVEL1_W`; only the first three fields are ever read."""

    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * WINSTATIONNAME_LENGTH),
        ("UserName", wintypes.WCHAR * (USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
        ("VmName", wintypes.WCHAR * WINSTATIONNAME_LENGTH),
    ]


class _WTSINFOEX_DATA(ctypes.Union):
    # Only level 1 is read; a level-2 answer is rejected by `info.Level != 1`.
    _fields_ = [("Level1", _WTSINFOEX_LEVEL1_W)]


class _WTSINFOEXW(ctypes.Structure):
    _fields_ = [("Level", wintypes.DWORD), ("Data", _WTSINFOEX_DATA)]


# Bytes of `WTSINFOEXW` that must be present before `SessionFlags` is readable.
_SESSION_FLAGS_END = (
    _WTSINFOEXW.Data.offset + _WTSINFOEX_LEVEL1_W.SessionFlags.offset + ctypes.sizeof(wintypes.LONG)
)


def session_flags() -> int | None:
    """`WTS_SESSIONSTATE_LOCK`/`_UNLOCK` for the current session, or None."""
    try:
        wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buffer = ctypes.c_void_p()
        size = wintypes.DWORD(0)
        ok = wtsapi32.WTSQuerySessionInformationW(
            None,
            WTS_CURRENT_SESSION,
            WTSSessionInfoEx,
            ctypes.byref(buffer),
            ctypes.byref(size),
        )
    except (AttributeError, OSError):
        return None
    if not ok or not buffer.value:
        return None
    try:
        if size.value < _SESSION_FLAGS_END:
            return None
        info = ctypes.cast(buffer, ctypes.POINTER(_WTSINFOEXW)).contents
        if info.Level != 1:
            return None
        return int(info.Data.Level1.SessionFlags)
    finally:
        try:
            wtsapi32.WTSFreeMemory(buffer)
        except OSError:  # pragma: no cover - defensive
            logger.debug("WTSFreeMemory failed", exc_info=True)


def input_desktop_locked() -> bool | None:
    """True when Winlogon owns the input desktop (i.e. the screen is locked)."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        desktop = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    except (AttributeError, OSError):
        return None
    if not desktop:
        # Access is denied while the secure desktop is up; anything else is
        # equally good evidence that there is no user desktop to speak into.
        return True
    try:
        return False
    finally:
        user32.CloseDesktop(desktop)


def is_workstation_locked() -> bool:
    """Best-effort, never-raising answer to "is the screen locked?"."""
    flags = session_flags()
    if flags is not None:
        return flags == WTS_SESSIONSTATE_LOCK
    locked = input_desktop_locked()
    return bool(locked)


class WindowsSessionState:
    """`bark_pc.platform.base.SessionState` for Windows."""

    name = "windows"

    def is_locked(self) -> bool:
        return is_workstation_locked()
