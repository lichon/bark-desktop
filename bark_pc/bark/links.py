"""Bark link lifecycle: register, rotate, verify, and honest abandonment.

The server has no unregister endpoint, so switching servers cannot delete the old
key.  The app records the old link as `abandoned` and tells the user plainly that
the key will only stop receiving once this device stops acknowledging it.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace

from ..core.config import MAX_SUPABASE_KEY_LENGTH, is_supabase_url
from ..core.errors import BarkError, LinkInvalid, ServerRejected
from ..core.models import BarkLink, LinkState, now_ms
from .server_client import BarkServerClient, normalize_server_url

logger = logging.getLogger("bark_pc.links")

DEVICE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9\-._~]{1,160}$")
MAX_KEY_LENGTH = 160
ABANDON_NOTE = (
    "The server cannot delete a device key. The old key will only stop receiving "
    "messages once this device stops acknowledging them; the app never claims the "
    "key was revoked."
)


def validate_device_key(key: str) -> str:
    text = (key or "").strip()
    if not text:
        raise LinkInvalid("The device key is empty.")
    if len(text) > MAX_KEY_LENGTH:
        raise LinkInvalid(f"The device key is longer than {MAX_KEY_LENGTH} characters.")
    if not DEVICE_KEY_PATTERN.match(text):
        raise LinkInvalid("The device key contains characters that are not URL-safe.")
    return text


def validate_supabase_key(key: str) -> str:
    """Supabase anon/publishable keys are JWT-like and far longer than Bark keys."""
    text = (key or "").strip()
    if not text:
        raise LinkInvalid("The Supabase auth key is empty.")
    if len(text) > MAX_SUPABASE_KEY_LENGTH:
        raise LinkInvalid(
            f"The Supabase auth key is longer than {MAX_SUPABASE_KEY_LENGTH} characters."
        )
    if any(character.isspace() for character in text):
        raise LinkInvalid("The Supabase auth key must not contain whitespace.")
    return text


@dataclass
class SwitchResult:
    link: BarkLink
    abandoned: BarkLink | None = None
    note: str | None = None


def push_url(server_url: str, device_key: str) -> str:
    return f"{server_url.rstrip('/')}/{device_key}"


def share_payload(link: BarkLink) -> str:
    """The push URL doubles as the share/QR payload: senders can use it directly."""
    if not link.server_url or not link.device_key:
        return ""
    if link.is_supabase:
        validate_supabase_key(link.device_key)
    else:
        validate_device_key(link.device_key)
    return push_url(link.server_url, link.device_key)


class LinkManager:
    """Applies link state transitions against the `bark_link` singleton row."""

    def __init__(self, db, *, client_factory=BarkServerClient, push_topic: str = "me.fin.bark"):
        self.db = db
        self.client_factory = client_factory
        self.push_topic = push_topic

    # -- registration ------------------------------------------------------ #
    def register(
        self,
        server_url: str,
        device_token: str,
        *,
        device_key: str | None = None,
        push_topic: str | None = None,
    ) -> BarkLink:
        normalized = normalize_server_url(server_url)
        if is_supabase_url(normalized):
            return self.register_supabase(
                normalized,
                device_key or "",
                push_topic=push_topic,
            )
        if device_key:
            validate_device_key(device_key)
        previous = self.db.get_link()
        same_server = bool(previous and previous.server_url == normalized and previous.device_key)
        key_to_send = device_key or (previous.device_key if same_server else None)
        topic = push_topic or (previous.push_topic if previous else self.push_topic)

        if previous is None:
            # Reflect the in-flight registration without touching a working link.
            self.db.upsert_link(
                BarkLink(
                    server_url=normalized,
                    device_key="",
                    push_topic=topic,
                    state=LinkState.REGISTERING,
                    created_at=now_ms(),
                    updated_at=now_ms(),
                )
            )

        client = self.client_factory(normalized)
        try:
            key = client.register(device_token, device_key=key_to_send)
        except BarkError as exc:
            logger.warning("registration on %s failed: %s", normalized, exc)
            if previous is not None and previous.is_usable:
                # Keep the previous link usable; only record the failure.
                restored = replace(
                    previous,
                    state=LinkState.LINKED,
                    last_error=str(exc),
                    updated_at=now_ms(),
                )
                self.db.upsert_link(restored)
            else:
                self.db.upsert_link(
                    BarkLink(
                        server_url=normalized,
                        device_key="",
                        push_topic=topic,
                        state=LinkState.STALE,
                        created_at=(previous.created_at if previous else now_ms()),
                        updated_at=now_ms(),
                        last_error=str(exc),
                    )
                )
            raise
        finally:
            client.close()

        validate_device_key(key)
        created = previous.created_at if previous and previous.created_at else now_ms()
        link = BarkLink(
            server_url=normalized,
            device_key=key,
            push_topic=topic,
            state=LinkState.LINKED,
            created_at=created,
            updated_at=now_ms(),
            last_verified_at=now_ms(),
            last_error=None,
        )
        # Single atomic write: the new key and server become visible together.
        self.db.upsert_link(link)
        logger.info("link registered with %s", normalized)
        return link

    def register_supabase(
        self,
        server_url: str,
        auth_key: str,
        *,
        push_topic: str | None = None,
    ) -> BarkLink:
        """Record a Supabase Realtime link; no Apple activation/registration is involved."""
        normalized = normalize_server_url(server_url)
        if not is_supabase_url(normalized):
            raise LinkInvalid("Supabase registration requires an sb:// server address.")
        key = validate_supabase_key(auth_key)
        previous = self.db.get_link()
        topic = push_topic or (previous.push_topic if previous else self.push_topic)
        created = previous.created_at if previous and previous.created_at else now_ms()
        link = BarkLink(
            server_url=normalized,
            device_key=key,
            push_topic=topic,
            state=LinkState.LINKED,
            created_at=created,
            updated_at=now_ms(),
            last_verified_at=now_ms(),
            last_error=None,
        )
        self.db.upsert_link(link)
        logger.info("Supabase Realtime link registered with %s (room %s)", normalized, topic)
        return link

    def rotate(self, device_token: str, *, server_url: str | None = None) -> BarkLink:
        """Re-register the current key on the same server (server overwrites token)."""
        current = self.db.get_link()
        if current is None or not current.server_url:
            raise LinkInvalid("There is no link to rotate.")
        target = server_url or current.server_url
        if normalize_server_url(target) != normalize_server_url(current.server_url):
            raise LinkInvalid("Rotation must target the same server.")
        return self.register(target, device_token, device_key=current.device_key)

    def switch_server(
        self, new_url: str, device_token: str, *, device_key: str | None = None
    ) -> SwitchResult:
        normalized = normalize_server_url(new_url)
        previous = self.db.get_link()
        differs = bool(previous and previous.server_url and previous.server_url != normalized)
        abandoned: BarkLink | None = None
        if differs and previous is not None:
            abandoned = replace(
                previous,
                state=LinkState.ABANDONED,
                updated_at=now_ms(),
                abandoned_at=now_ms(),
            )
            # Mark it abandoned while the new registration is in flight so the
            # state is never misreported as still active.
            self.db.upsert_link(abandoned)
        try:
            link = self.register(normalized, device_token, device_key=device_key)
        except BarkError:
            if abandoned is not None and previous is not None:
                # The switch failed: restore the still-working link.
                self.db.upsert_link(previous)
            raise
        if abandoned is not None:
            return SwitchResult(link=link, abandoned=abandoned, note=ABANDON_NOTE)
        return SwitchResult(link=link)

    # -- verification ------------------------------------------------------ #
    def verify(self, link: BarkLink | None = None) -> bool:
        current = link or self.db.get_link()
        if current is None or not current.device_key:
            return False
        if current.is_supabase:
            # A Realtime subscription has no Bark-side key to verify; the live
            # WebSocket and its heartbeat are the connection check.
            self.db.mark_link_state(
                LinkState.LINKED, last_error=None, last_verified_at=now_ms()
            )
            return True
        client = self.client_factory(current.server_url)
        try:
            ok = client.verify_key(current.device_key)
        except ServerRejected as exc:
            self.db.mark_link_state(LinkState.STALE, last_error=str(exc))
            raise
        except BarkError as exc:
            # A transport failure says nothing about key validity.
            self.db.upsert_link(replace(current, last_error=str(exc), updated_at=now_ms()))
            raise
        finally:
            client.close()
        if ok:
            self.db.mark_link_state(LinkState.LINKED, last_error=None, last_verified_at=now_ms())
        else:
            self.db.mark_link_state(LinkState.STALE, last_error="the server does not know this key")
        return ok

    # -- derived values ---------------------------------------------------- #
    @staticmethod
    def push_url(link: BarkLink) -> str:
        return push_url(link.server_url, link.device_key)

    @staticmethod
    def share_payload(link: BarkLink) -> str:
        return share_payload(link)


def link_is_usable(link: BarkLink | None) -> bool:
    return bool(link and link.is_usable)


def utc_now_s() -> float:
    return time.time()
