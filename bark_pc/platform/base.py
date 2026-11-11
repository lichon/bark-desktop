"""Platform abstraction protocols.

Only `bark_pc.platform.windows.*` may import Win32/WinRT.  Everything else talks
to these protocols, so macOS/Linux support means implementing them, not editing
the receiver, storage or UI.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretStore(Protocol):
    """Encrypt/decrypt small secret blobs that are tied to the current user."""

    def protect(self, data: bytes) -> bytes: ...

    def unprotect(self, data: bytes) -> bytes: ...

    def save(self, path: Path, data: bytes) -> None: ...

    def load(self, path: Path) -> bytes: ...


@runtime_checkable
class Tray(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def set_status(self, status: str, reason: str | None = None) -> None: ...

    def set_unread(self, count: int) -> None: ...

    def notify(self, title: str, message: str) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    def show(self, notification) -> bool: ...

    def remove(self, notification) -> None: ...


@runtime_checkable
class Autostart(Protocol):
    def is_enabled(self) -> bool: ...

    def enable(self, exe_path: str | Path) -> None: ...

    def disable(self) -> None: ...

    def stored_path(self) -> str | None: ...


@runtime_checkable
class SingleInstance(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...

    def focus_existing(self) -> bool: ...


@runtime_checkable
class SessionState(Protocol):
    """Report whether the interactive desktop is locked (screen locked)."""

    name: str

    def is_locked(self) -> bool: ...


@runtime_checkable
class Opener(Protocol):
    def open_url(self, url: str) -> bool: ...

    def open_folder(self, path: str | Path) -> bool: ...


@runtime_checkable
class UriHandler(Protocol):
    def register(self, exe_path: str | Path) -> None: ...

    def unregister(self) -> None: ...

    def parse(self, uri: str) -> dict: ...


class NullSecretStore:
    """Test/development store: raises on use so nothing silently persists plaintext."""

    def protect(self, data: bytes) -> bytes:
        raise NotImplementedError("no secret store configured for this platform")

    def unprotect(self, data: bytes) -> bytes:
        raise NotImplementedError("no secret store configured for this platform")

    def save(self, path: Path, data: bytes) -> None:
        raise NotImplementedError("no secret store configured for this platform")

    def load(self, path: Path) -> bytes:
        raise NotImplementedError("no secret store configured for this platform")


class CallbackTray:
    """Tray stub used in tests and when no shell is available."""

    def __init__(self):
        self.status = "starting"
        self.unread = 0
        self.messages: list[tuple[str, str]] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def stop(self) -> None:  # pragma: no cover - trivial
        pass

    def set_status(self, status: str, reason: str | None = None) -> None:
        self.status = status

    def set_unread(self, count: int) -> None:
        self.unread = int(count)

    def notify(self, title: str, message: str) -> None:
        self.messages.append((title, message))


class NullNotifier:
    """Notifier used in headless runs and tests."""

    def __init__(self, on_show: Callable | None = None):
        self.shown: list = []
        self.removed: list = []
        self._on_show = on_show

    def show(self, notification) -> bool:
        self.shown.append(notification)
        if self._on_show is not None:
            self._on_show(notification)
        return True

    def remove(self, notification) -> None:
        self.removed.append(notification)


class MemorySecretStore:
    """Deterministic in-memory 'encryption' for tests (base64 + prefix)."""

    PREFIX = b"MEMSECRET:"

    def protect(self, data: bytes) -> bytes:
        import base64

        return self.PREFIX + base64.b64encode(data)

    def unprotect(self, data: bytes) -> bytes:
        import base64

        if not data.startswith(self.PREFIX):
            from ..core.errors import IdentityUnreadable

            raise IdentityUnreadable()
        return base64.b64decode(data[len(self.PREFIX) :])

    def save(self, path: Path, data: bytes) -> None:
        from ..core.config import atomic_write_bytes

        atomic_write_bytes(path, self.protect(data))

    def load(self, path: Path) -> bytes:
        from ..core.errors import IdentityMissing

        if not path.exists():
            raise IdentityMissing()
        return self.unprotect(path.read_bytes())
