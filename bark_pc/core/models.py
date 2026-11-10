"""Domain models and enumerations shared by every layer.

This module is deliberately dependency-free (standard library only) so that the
core, push, bark, images and UI layers can all import it without pulling in
platform or third-party modules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

TOAST_MAX_CHARS = 200
BODY_MAX_STORED_BYTES = 8 * 1024
RAW_JSON_MAX_BYTES = 4 * 1024


def now_ms() -> int:
    """Current UTC time as epoch milliseconds (the storage format everywhere)."""
    return int(time.time() * 1000)


class DeviceState(str, Enum):
    ABSENT = "absent"
    ACTIVATING = "activating"
    ACTIVE = "active"
    INVALID = "invalid"


class LinkState(str, Enum):
    UNLINKED = "unlinked"
    REGISTERING = "registering"
    LINKED = "linked"
    STALE = "stale"
    ABANDONED = "abandoned"


class ImageState(str, Enum):
    PENDING = "pending"
    CACHED = "cached"
    UNAVAILABLE = "unavailable"
    TOO_LARGE = "too_large"
    NOT_IMAGE = "not_image"


class ConnectionStatus(str, Enum):
    STARTING = "starting"
    CONNECTING = "connecting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    INVALID = "invalid"
    SILENCED = "silenced"


class PushStatus(str, Enum):
    CONNECTING = "connecting"
    ACTIVE = "active"
    DEGRADED = "degraded"
    INVALID = "invalid"
    STOPPED = "stopped"


CONNECTION_STATUS_LABELS: dict[ConnectionStatus, str] = {
    ConnectionStatus.STARTING: "Starting",
    ConnectionStatus.CONNECTING: "Connecting…",
    ConnectionStatus.ACTIVE: "Connected",
    ConnectionStatus.DEGRADED: "Reconnecting…",
    ConnectionStatus.INVALID: "Action required",
    ConnectionStatus.SILENCED: "Silenced",
}


def truncate_text(text: str, limit: int = TOAST_MAX_CHARS) -> str:
    """Truncate *text* to *limit* characters, appending an ellipsis when cut."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def truncate_lines(text: str, limit: int = TOAST_MAX_CHARS) -> str:
    """Truncate every line of *text* independently (toast rule)."""
    if not text:
        return ""
    return "\n".join(truncate_text(line, limit) for line in text.splitlines())


@dataclass
class DeviceIdentity:
    """The anonymously activated emulated Apple device (singleton row)."""

    id: int = 1
    udid: str = ""
    serial: str = ""
    device_class: str = "Windows"
    cert_fingerprint: str | None = None
    state: DeviceState = DeviceState.ABSENT
    activated_at: int | None = None
    last_used_at: int | None = None
    last_error: str | None = None


@dataclass
class BarkLink:
    """Binding between this device and one Bark-compatible server (singleton row)."""

    id: int = 1
    server_url: str = ""
    device_key: str = ""
    push_topic: str = "me.fin.bark"
    state: LinkState = LinkState.UNLINKED
    created_at: int | None = None
    updated_at: int | None = None
    last_verified_at: int | None = None
    last_error: str | None = None
    abandoned_at: int | None = None

    @property
    def is_supabase(self) -> bool:
        return (self.server_url or "").strip().lower().startswith("sb://")

    @property
    def push_url(self) -> str:
        if not self.server_url or not self.device_key:
            return ""
        return f"{self.server_url.rstrip('/')}/{self.device_key}"

    @property
    def is_usable(self) -> bool:
        return bool(self.server_url and self.device_key) and self.state in (
            LinkState.LINKED,
            LinkState.STALE,
        )


@dataclass
class Notification:
    """A message received while the app was connected."""

    id: int | None = None
    remote_id: str | None = None
    title: str = ""
    subtitle: str | None = None
    body: str = ""
    group_name: str | None = None
    url: str | None = None
    level: str | None = None
    image_url: str | None = None
    image_id: int | None = None
    raw_json: str | None = None
    received_at: int = field(default_factory=now_ms)
    read_at: int | None = None
    deleted_at: int | None = None
    source_server: str = ""
    # True when this desktop sent the message itself (Realtime compose bar).
    # Self messages are stored already read and never toasted or spoken.
    from_self: bool = False

    @property
    def display_title(self) -> str:
        title = (self.title or "").strip()
        subtitle = (self.subtitle or "").strip()
        if title and subtitle:
            return f"{title} — {subtitle}"
        return title or subtitle

    @property
    def toast_title(self) -> str:
        return truncate_lines(self.display_title or "Bark")

    @property
    def toast_body(self) -> str:
        return truncate_lines(self.body or "")

    @property
    def effective_image_url(self) -> str | None:
        return self.image_url or None


@dataclass
class ImageCacheEntry:
    """Row in the content-addressed image cache."""

    id: int | None = None
    url_hash: str = ""
    url: str = ""
    path: str = ""
    bytes: int = 0
    width: int = 0
    height: int = 0
    thumb_path: str = ""
    state: ImageState = ImageState.PENDING
    fetched_at: int | None = None
    last_used_at: int | None = None

    @property
    def is_cached(self) -> bool:
        return self.state == ImageState.CACHED


@dataclass
class ActivationMaterial:
    """Activation secrets; never logged, always redacted in repr."""

    cert_pem: str = ""
    key_pem: str = ""
    base_token_hex: str | None = None
    udid: str = ""
    serial: str = ""

    def __repr__(self) -> str:  # pragma: no cover - trivial but security relevant
        token = "<unset>"
        if self.base_token_hex:
            token = f"{self.base_token_hex[:8]}…"
        return (
            f"ActivationMaterial(udid={self.udid!r}, serial={self.serial!r}, "
            f"cert=<redacted>, key=<redacted>, base_token={token})"
        )

    __str__ = __repr__

    def to_json(self) -> dict[str, str | None]:
        return {
            "cert_pem": self.cert_pem,
            "key_pem": self.key_pem,
            "base_token_hex": self.base_token_hex,
            "udid": self.udid,
            "serial": self.serial,
        }

    @classmethod
    def from_json(cls, data: dict) -> ActivationMaterial:
        return cls(
            cert_pem=str(data.get("cert_pem") or ""),
            key_pem=str(data.get("key_pem") or ""),
            base_token_hex=data.get("base_token_hex") or None,
            udid=str(data.get("udid") or ""),
            serial=str(data.get("serial") or ""),
        )

    def redacted(self) -> dict[str, str | None]:
        return {
            "udid": self.udid,
            "serial": self.serial,
            "base_token_hex": (self.base_token_hex or "")[:8] + "…"
            if self.base_token_hex
            else None,
        }
