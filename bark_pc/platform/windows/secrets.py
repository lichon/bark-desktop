"""Windows DPAPI credential protection (per-user, no admin rights).

The activation blob is encrypted with `CryptProtectData` scoped to the current
Windows account.  Copying the portable folder to another account or machine
yields bytes that cannot be decrypted, which the app detects and reports as a
missing identity instead of failing hard.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path

from ...core.errors import IdentityMissing, IdentityUnreadable

logger = logging.getLogger("bark_pc.secrets")

CRYPTPROTECT_UI_FORBIDDEN = 0x01
DESCRIPTION = "Bark Desktop Client"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _as_blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _blob_bytes(blob: DATA_BLOB) -> bytes:
    try:
        return ctypes.string_at(blob.pbData, blob.cbData)
    finally:
        if blob.pbData:
            ctypes.windll.kernel32.LocalFree(blob.pbData)


def _require_windows() -> None:
    if sys.platform != "win32":
        raise IdentityUnreadable(
            "DPAPI is only available on Windows; no secret store is configured."
        )


class WindowsSecretStore:
    """`SecretStore` implementation backed by DPAPI."""

    def protect(self, data: bytes) -> bytes:
        _require_windows()
        blob_in, _buffer = _as_blob(data)
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            DESCRIPTION,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise IdentityUnreadable(f"CryptProtectData failed (error {ctypes.GetLastError()}).")
        return _blob_bytes(blob_out)

    def unprotect(self, data: bytes) -> bytes:
        _require_windows()
        blob_in, _buffer = _as_blob(data)
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(blob_out),
        )
        if not ok:
            raise IdentityUnreadable(
                "The saved device identity cannot be decrypted by this Windows account."
            )
        return _blob_bytes(blob_out)

    def save(self, path: Path, data: bytes) -> None:
        from ...core.config import atomic_write_bytes

        try:
            protected = self.protect(data)
        except IdentityUnreadable:
            raise
        atomic_write_bytes(Path(path), protected)

    def load(self, path: Path) -> bytes:
        path = Path(path)
        if not path.exists():
            raise IdentityMissing()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise IdentityUnreadable(f"Cannot read {path.name}: {exc}") from exc
        if not raw:
            raise IdentityUnreadable(f"{path.name} is empty")
        return self.unprotect(raw)


def get_store() -> WindowsSecretStore:
    """Factory used by the composition root (keeps Win32 imports local)."""
    return WindowsSecretStore()
