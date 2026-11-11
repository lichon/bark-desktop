"""Supabase Realtime transport (`sb://` server addresses).

Supabase Realtime is a Phoenix-Channels WebSocket service.  This adapter joins
the same room shape used by the companion `use-supabase.ts` hook
(`room:<roomName>:messages`), listens for `message`/`event` broadcasts, and
feeds their Bark payloads into the normal ingest path.

Only this module imports the `websockets` client.  Authentication is the
Supabase anon/publishable key, sent as the `apikey` query parameter — the same
credential the JavaScript client uses.  The key is never logged.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit

import anyio
import httpx

from ..core.config import (
    DEFAULT_PUSH_TOPIC,
    is_supabase_url,
    supabase_base_url,
    validate_server_url,
)
from ..core.errors import (
    BarkError,
    CourierRejected,
    ServerRejected,
    ServerTimeout,
    ServerUnreachable,
    StreamLost,
)
from ..core.http_debug import client_event_hooks
from ..core.models import PushStatus
from .payload import SELF_FIELD

try:  # websockets >= 13
    from websockets.asyncio.client import connect as _websocket_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
except ImportError:  # pragma: no cover - websockets 12 compatibility
    from websockets.client import connect as _websocket_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

logger = logging.getLogger("bark_pc.supabase")

PROTOCOL_VERSION = "1.0.0"
HEARTBEAT_INTERVAL_S = 25.0
JOIN_TIMEOUT_S = 10.0
OPEN_TIMEOUT_S = 10.0
PUBLISH_SETTLE_S = 0.2

# Phoenix control events, plus the app-level `command` event used by the
# companion hook for request/response — none of them are notifications.
_CONTROL_EVENTS = frozenset(
    {
        "phx_reply",
        "phx_error",
        "phx_close",
        "presence_state",
        "presence_diff",
        "heartbeat",
        "access_token",
        "system",
        "command",
    }
)
_MESSAGE_EVENTS = frozenset({"message", "event", "broadcast"})

# Fallback `sender` label when the host name cannot be read.  Own messages are
# recognised by their message id (see `_sent_ids`), never by this label, so a
# room where someone else is also called this does not confuse the client.
FALLBACK_SENDER = "Bark Desktop"

# How long a published message id counts as "ours", and how many ids to keep.
SENT_ID_TTL_S = 300.0
SENT_ID_MAX = 512


def default_sender() -> str:
    """The `sender` label of broadcasts from this machine: its host name."""
    try:
        name = socket.gethostname().strip()
    except OSError:  # pragma: no cover - hostname lookup failure
        name = ""
    return name or FALLBACK_SENDER


class RecentSentIds:
    """Ids of the broadcasts this process published recently.

    Publishing uses a short-lived connection while the streaming receiver holds
    another one, and Realtime echoes a broadcast back to every connection in the
    room — including our own.  Remembering the ids we sent is how the receiver
    tells our frames apart from everyone else's; the `sender` label is just a
    display name and cannot be trusted for that.  Entries expire after `ttl`
    seconds and the set is bounded, so it cannot grow without limit.
    """

    def __init__(
        self,
        *,
        ttl: float = SENT_ID_TTL_S,
        maxsize: int = SENT_ID_MAX,
        clock=time.monotonic,
    ):
        self.ttl = max(1.0, float(ttl))
        self.maxsize = max(1, int(maxsize))
        self._clock = clock
        self._ids: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def remember(self, message_id: str | None) -> None:
        text = (message_id or "").strip()
        if not text:
            return
        now = self._clock()
        with self._lock:
            self._purge(now)
            self._ids[text] = now
            self._ids.move_to_end(text)
            while len(self._ids) > self.maxsize:
                self._ids.popitem(last=False)

    def __contains__(self, message_id: object) -> bool:
        if not isinstance(message_id, str):
            return False
        text = message_id.strip()
        if not text:
            return False
        now = self._clock()
        with self._lock:
            self._purge(now)
            return text in self._ids

    def __len__(self) -> int:
        with self._lock:
            self._purge(self._clock())
            return len(self._ids)

    def clear(self) -> None:
        with self._lock:
            self._ids.clear()

    def _purge(self, now: float) -> None:
        while self._ids:
            _, seen_at = next(iter(self._ids.items()))
            if now - seen_at <= self.ttl:
                return
            self._ids.popitem(last=False)


_sent_ids = RecentSentIds()


# --------------------------------------------------------------------------- #
# Topic + URL helpers
# --------------------------------------------------------------------------- #
def room_topic(room_name: str | None) -> str:
    """The broadcast room used by `use-supabase.ts`: `room:<name>:messages`."""
    name = (room_name or "").strip() or DEFAULT_PUSH_TOPIC
    return f"room:{name}:messages"


def channel_topic(room_name: str | None) -> str:
    """Phoenix topic name (`realtime:` prefix) for a Realtime channel."""
    return f"realtime:{room_topic(room_name)}"


def realtime_ws_url(server_url: str, auth_key: str) -> str:
    """Build the Phoenix WebSocket endpoint for a `sb://` address.

    `sb://` becomes `wss://` for hosted projects and `ws://` for loopback
    development projects.  The auth key travels in the query string, exactly as
    the official Supabase client does.
    """
    base = supabase_base_url(server_url)
    parts = urlsplit(base)
    scheme = "ws" if parts.scheme == "http" else "wss"
    path = f"{parts.path.rstrip('/')}/realtime/v1/websocket"
    query = urlencode({"apikey": auth_key, "vsn": PROTOCOL_VERSION})
    return urlunsplit((scheme, parts.netloc, path, query, ""))


# --------------------------------------------------------------------------- #
# Broadcast payload → Bark payload
# --------------------------------------------------------------------------- #
def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _coerce_content(content) -> dict | None:
    """Turn the hook's `ChannelMessage.content` into a Bark payload object.

    Our own frames put the notification *body* there as a plain string; other
    `use-supabase.ts` senders may put a `{title, body}` object instead.  A string
    is always the body — it is never decoded as JSON, so a body that happens to
    look like an object still shows up as text.
    """
    if content is None:
        return None
    if isinstance(content, dict):
        return dict(content)
    text = str(content).strip()
    if not text:
        return None
    return {"aps": {"alert": {"body": text}}}


def _content_text(payload: dict) -> str:
    """The `content` string of an outgoing `ChannelMessage`: the alert text.

    Realtime rooms carry a one-line message, not a push envelope, so only the
    human-readable part of the payload is broadcast.  Bark nests it under
    `aps.alert`; bare `{title, body}` objects are supported too.
    """
    aps = payload.get("aps")
    alert = aps.get("alert") if isinstance(aps, dict) else None
    if isinstance(alert, str) and alert.strip():
        return alert.strip()
    if isinstance(alert, dict):
        for key in ("body", "title", "subtitle"):
            text = str(alert.get(key) or "").strip()
            if text:
                return text
    for key in ("body", "title"):
        text = str(payload.get(key) or "").strip()
        if text:
            return text
    return ""


def _apply_sender_title(payload: dict, sender: str) -> None:
    """Use the `ChannelMessage` sender as the title when the content has none.

    `use-supabase.ts` sends `{id, type, content, timestamp, sender}` where the
    sender is the human label of the sender and content is just the message
    text.  Without this, such notifications would render as a bare "Bark".
    """
    aps = payload.get("aps")
    if not isinstance(aps, dict):
        return
    alert = aps.get("alert")
    if not isinstance(alert, dict):
        return
    if str(alert.get("title") or "").strip():
        return
    alert["title"] = sender


def _apply_room_group(payload: dict, room: str | None) -> None:
    """Use the Realtime room name as the notification group.

    Room membership is the natural grouping for Supabase messages (toasts per
    room, the history's *Group* line); an explicit `group` in the content always
    wins, and recall payloads are left untouched.
    """
    name = (room or "").strip()
    if not name or "delete" in payload or str(payload.get("group") or "").strip():
        return
    payload["group"] = name


def _looks_like_bark(payload) -> bool:
    return isinstance(payload, dict) and any(
        key in payload for key in ("aps", "delete", "ciphertext", "title", "body")
    )


def _bark_payload(content, envelope: dict | None = None, room: str | None = None) -> bytes | None:
    payload = _coerce_content(content)
    if payload is None:
        return None

    # A plain `{title, body}` object is the most common thing a companion app
    # sends.  Bark itself expects those fields nested under `aps.alert`.
    if "aps" not in payload and "delete" not in payload:
        alert = {}
        for key in ("title", "subtitle", "body"):
            if key in payload:
                alert[key] = payload.pop(key)
        if alert:
            payload["aps"] = {"alert": alert}
        elif "message" in payload:
            payload["aps"] = {"alert": {"body": str(payload.pop("message"))}}
        elif "ciphertext" not in payload:
            # Unknown object: keep the content readable instead of dropping it.
            payload = {"aps": {"alert": {"body": json.dumps(payload, ensure_ascii=False)}}}

    if envelope:
        if not payload.get("id"):
            envelope_id = envelope.get("id")
            if envelope_id:
                payload["id"] = str(envelope_id)
        sender = str(envelope.get("sender") or "").strip()
        if sender:
            payload.setdefault("sender", sender)
            _apply_sender_title(payload, sender)
    _apply_room_group(payload, room)
    if payload.get("id") in _sent_ids:
        # Our own broadcast: flag it so ingest stores it read and stays silent.
        payload[SELF_FIELD] = "1"
    return _json_bytes(payload)


def payload_from_broadcast(event, payload, *, room: str | None = None) -> bytes | None:
    """Convert one Realtime broadcast frame into a Bark payload, or None.

    Supports both wire shapes seen in the wild:

    * legacy/simple: Phoenix event is the custom name (`message`) and the payload
      is the `ChannelMessage`;
    * Supabase broadcast envelope: event `broadcast` with
      `{type: "broadcast", event: "message", payload: …}`.

    `room` is the room the frame arrived on; it becomes the notification group
    when the payload does not carry one.
    """
    if isinstance(payload, dict) and payload.get("type") == "broadcast":
        event = payload.get("event") or event
        payload = payload.get("payload")

    event_text = str(event or "").lower()
    if event_text in _CONTROL_EVENTS:
        return None
    if event_text in _MESSAGE_EVENTS:
        if isinstance(payload, dict):
            message_type = str(payload.get("type") or "message").lower()
            if message_type == "command":
                return None
            if "content" in payload:
                return _bark_payload(payload.get("content"), payload, room)
        return _bark_payload(payload, room=room)
    # A custom event name can still carry a complete Bark payload; accept only
    # shapes that are unmistakably Bark to avoid turning typing/presence-style
    # events into notifications.
    if _looks_like_bark(payload):
        return _bark_payload(payload, room=room)
    return None


# --------------------------------------------------------------------------- #
# Phoenix helpers
# --------------------------------------------------------------------------- #
def _decode_frame(raw) -> dict | None:
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _reply_error(payload) -> str:
    if not isinstance(payload, dict):
        return "unknown error"
    response = payload.get("response")
    if isinstance(response, dict):
        for key in ("reason", "message", "error"):
            value = response.get(key)
            if value:
                return str(value)
    for key in ("reason", "message", "error"):
        value = payload.get(key)
        if value:
            return str(value)
    return "unknown error"


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Pick the useful member out of an ExceptionGroup produced by anyio."""
    while True:
        children = getattr(exc, "exceptions", None)
        if not children:
            return exc
        if len(children) == 1:
            exc = children[0]
            continue
        for child in children:
            if isinstance(child, BarkError):
                return _unwrap_exception(child)
        for child in children:
            if "cancel" not in type(child).__name__.lower():
                return _unwrap_exception(child)
        return exc


def _map_error(exc: BaseException) -> BaseException:
    leaf = _unwrap_exception(exc)
    if isinstance(leaf, BarkError):
        return leaf
    if isinstance(leaf, InvalidStatus):
        response = getattr(leaf, "response", None)
        status = getattr(response, "status_code", 0)
        if status in (401, 403):
            return CourierRejected(
                "Supabase Realtime rejected the auth key. Check the anon/publishable key."
            )
        if status in (400, 404):
            return CourierRejected(
                f"Supabase Realtime refused the connection (HTTP {status}). "
                "Check the project URL."
            )
        return StreamLost(f"Supabase Realtime handshake failed (HTTP {status}).")
    if isinstance(leaf, ConnectionClosed):
        return StreamLost("Supabase Realtime closed the connection.")
    if isinstance(leaf, TimeoutError):
        return StreamLost("Supabase Realtime did not respond in time.")
    if isinstance(leaf, (WebSocketException, OSError)):
        return StreamLost(f"Supabase Realtime connection failed: {leaf}")
    return StreamLost(f"{type(leaf).__name__}: {leaf}")


async def _stop_quietly(ws) -> None:
    try:
        await ws.close()
    except Exception:  # pragma: no cover - already closed
        pass


# --------------------------------------------------------------------------- #
# Receiver
# --------------------------------------------------------------------------- #
class SupabaseRealtimeReceiver:
    """`PushReceiver` implementation backed by Supabase Realtime broadcasts."""

    # The supervisor must not demand an Apple activation material for this link.
    needs_material = False

    def __init__(
        self,
        *,
        server_url: str,
        auth_key: str,
        room_name: str | None = None,
        connect=None,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        join_timeout: float = JOIN_TIMEOUT_S,
        open_timeout: float = OPEN_TIMEOUT_S,
        publish_settle: float = PUBLISH_SETTLE_S,
    ):
        self.server_url = validate_server_url(server_url)
        if not is_supabase_url(self.server_url):
            raise ValueError("Supabase Realtime requires an sb:// server address")
        self.auth_key = (auth_key or "").strip()
        if not self.auth_key:
            raise ValueError("Supabase Realtime requires an auth key")
        self.room_name = (room_name or "").strip() or DEFAULT_PUSH_TOPIC
        self._connect = connect or _websocket_connect
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        self.join_timeout = max(1.0, float(join_timeout))
        self.open_timeout = max(1.0, float(open_timeout))
        self.publish_settle = max(0.0, float(publish_settle))
        self._heartbeat_ref = 0

    # -- connection -------------------------------------------------------- #
    @asynccontextmanager
    async def _connection(self):
        url = realtime_ws_url(self.server_url, self.auth_key)
        async with self._connect(
            url,
            open_timeout=self.open_timeout,
            close_timeout=5.0,
            ping_interval=None,  # we speak the Phoenix heartbeat ourselves
            max_size=None,
        ) as ws:
            yield ws

    async def _join(self, ws, room_name: str, *, on_activity=None) -> tuple[str, list[dict]]:
        join_ref = uuid.uuid4().hex
        await ws.send(
            json.dumps(
                {
                    "topic": channel_topic(room_name),
                    "event": "phx_join",
                    "payload": {
                        "config": {
                            "broadcast": {"self": False},
                            "presence": {"key": ""},
                            "private": False,
                        }
                    },
                    "ref": join_ref,
                    "join_ref": join_ref,
                }
            )
        )
        buffered: list[dict] = []
        try:
            with anyio.fail_after(self.join_timeout):
                while True:
                    raw = await ws.recv()
                    self._activity(on_activity)
                    data = _decode_frame(raw)
                    if data is None:
                        continue
                    if data.get("event") == "phx_reply" and data.get("ref") == join_ref:
                        payload = data.get("payload")
                        if isinstance(payload, dict) and payload.get("status") == "ok":
                            return join_ref, buffered
                        raise CourierRejected(
                            "Supabase Realtime refused the subscription: "
                            f"{_reply_error(payload)}"
                        )
                    buffered.append(data)
        except TimeoutError as exc:
            raise StreamLost(
                "Timed out joining the Supabase Realtime room. "
                "Check the project URL, auth key and room name."
            ) from exc

    async def _heartbeat_loop(self, ws) -> None:
        while True:
            await anyio.sleep(self.heartbeat_interval)
            self._heartbeat_ref += 1
            try:
                await ws.send(
                    json.dumps(
                        {
                            "topic": "phoenix",
                            "event": "heartbeat",
                            "payload": {},
                            "ref": str(self._heartbeat_ref),
                        }
                    )
                )
            except Exception as exc:  # pragma: no cover - transport already gone
                logger.debug("supabase: heartbeat failed: %s", exc)
                await _stop_quietly(ws)
                return

    # -- streaming --------------------------------------------------------- #
    async def run(
        self,
        *,
        material=None,
        topic: str | None = None,
        on_message,
        on_status=None,
        on_token=None,
        on_activity=None,
    ) -> None:
        """Join the room, stream broadcasts, and keep the link alive.

        `material` and `on_token` exist only to match the receiver protocol used
        by the APNs adapter; Supabase has no device activation or push token.
        """
        status = on_status or (lambda _status, _reason: None)
        room_name = (topic or self.room_name).strip() or self.room_name
        host = urlsplit(supabase_base_url(self.server_url)).hostname or self.server_url
        logger.debug("supabase: connecting to %s (room=%s)", host, room_name)
        status(PushStatus.CONNECTING, None)
        try:
            async with self._connection() as ws:
                _join_ref, buffered = await self._join(
                    ws, room_name, on_activity=on_activity
                )
                status(PushStatus.ACTIVE, None)
                logger.info("supabase: active (room=%s)", room_name)
                for data in buffered:
                    self._dispatch(data, on_message, room_name)
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(self._heartbeat_loop, ws)
                    try:
                        while True:
                            raw = await ws.recv()
                            self._activity(on_activity)
                            data = _decode_frame(raw)
                            if data is None:
                                continue
                            self._dispatch(data, on_message, room_name)
                    finally:
                        task_group.cancel_scope.cancel()
        except anyio.get_cancelled_exc_class():
            raise
        except BarkError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to the app taxonomy
            mapped = _map_error(exc)
            logger.info("supabase: stream ended: %s", mapped)
            raise mapped from exc

    def _dispatch(self, data: dict, on_message, room: str | None = None) -> None:
        event = data.get("event")
        payload = data.get("payload")
        if event == "phx_reply":
            if isinstance(payload, dict) and payload.get("status") not in (None, "ok"):
                logger.warning("supabase: request failed: %s", _reply_error(payload))
            return
        if event in ("phx_error", "phx_close"):
            raise StreamLost(f"Supabase Realtime channel closed: {_reply_error(payload)}")
        if event == "system":
            if isinstance(payload, dict) and payload.get("status") == "error":
                raise StreamLost(f"Supabase Realtime channel error: {_reply_error(payload)}")
            return
        bark = payload_from_broadcast(event, payload, room=room)
        if bark is None:
            return
        try:
            on_message(bark)
        except Exception:  # pragma: no cover - callback isolation
            logger.exception("supabase: on_message callback failed")

    # -- sending (self-test) ----------------------------------------------- #
    async def publish(
        self,
        payload: dict,
        *,
        event: str = "message",
        room_name: str | None = None,
        sender: str | None = None,
    ) -> None:
        """Broadcast one message into the room (used by the self-test/

        The frame is a `ChannelMessage` exactly as `use-supabase.ts` builds it
        (`newMessage`): `{id, type, content, timestamp, sender}`.  `content` is
        the payload's alert text as a plain string — the room carries the
        message, not a push envelope — and `sender` becomes its title on the
        receiving side.  It defaults to this machine's host name.

        The generated message id is remembered as ours; the receiving side uses
        that set, not the sender label, to recognise the echo of this frame.
        """
        content = _content_text(payload)
        if not content:
            logger.debug("supabase: not publishing a payload without alert text")
            return
        room = (room_name or self.room_name).strip() or self.room_name
        message_id = uuid.uuid4().hex
        message = {
            "id": message_id,
            "type": "message",
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sender": (sender or "").strip() or default_sender(),
        }
        # Remember before sending: the echo can arrive on the streaming
        # connection while this frame is still in flight.
        _sent_ids.remember(message_id)
        try:
            async with self._connection() as ws:
                join_ref, _buffered = await self._join(ws, room)
                await ws.send(
                    json.dumps(
                        {
                            "topic": channel_topic(room),
                            "event": "broadcast",
                            "payload": {
                                "type": "broadcast",
                                "event": event,
                                "payload": message,
                            },
                            "ref": uuid.uuid4().hex,
                            "join_ref": join_ref,
                        }
                    )
                )
                await anyio.sleep(self.publish_settle)
        except BarkError:
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to the app taxonomy
            mapped = _map_error(exc)
            logger.info("supabase: publish failed: %s", mapped)
            raise mapped from exc

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _activity(on_activity) -> None:
        if on_activity is None:
            return
        try:
            on_activity()
        except Exception:  # pragma: no cover - callback isolation
            logger.exception("supabase: on_activity callback failed")


# --------------------------------------------------------------------------- #
# Synchronous helpers for the UI
# --------------------------------------------------------------------------- #
def publish_bark_payload(
    server_url: str,
    auth_key: str,
    room_name: str,
    payload: dict,
    *,
    sender: str | None = None,
    timeout: float = JOIN_TIMEOUT_S,
    connect=None,
) -> None:
    """Broadcast *payload* from a short-lived connection (blocking)."""
    receiver = SupabaseRealtimeReceiver(
        server_url=server_url,
        auth_key=auth_key,
        room_name=room_name,
        connect=connect,
        join_timeout=timeout,
        open_timeout=timeout,
    )

    async def publish_once() -> None:
        with anyio.fail_after(max(1.0, timeout) * 2):
            await receiver.publish(payload, sender=sender)

    try:
        anyio.run(publish_once)
    except BarkError:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to the app taxonomy
        raise _map_error(exc) from exc


def ping(server_url: str, auth_key: str, *, timeout: float = 10.0) -> bool:
    """Check that the Supabase project and auth key are usable (blocking)."""
    if not (auth_key or "").strip():
        raise ServerRejected(401, "The Supabase auth key is missing.")
    base = supabase_base_url(server_url)
    host = urlsplit(base).hostname or base
    url = f"{base.rstrip('/')}/rest/v1/"
    headers = {
        "apikey": auth_key,
        "Authorization": f"Bearer {auth_key}",
    }
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            event_hooks=client_event_hooks(),
        ) as client:
            response = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise ServerTimeout(detail=str(exc)) from exc
    except httpx.TransportError as exc:
        raise ServerUnreachable(host, detail=str(exc)) from exc
    if response.status_code in (401, 403):
        raise ServerRejected(
            response.status_code,
            "The Supabase auth key was rejected. Check the anon/publishable key.",
        )
    if response.status_code >= 400:
        raise ServerUnreachable(
            host, f"Cannot reach the Supabase project (HTTP {response.status_code})."
        )
    return True
