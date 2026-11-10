"""Supabase Realtime transport: URL shape, payload mapping, receiver, links.

No test opens a socket: the WebSocket factory is replaced with a scripted fake
and the HTTP client is never constructed for `sb://` registration.
"""

from __future__ import annotations

import json

import anyio
import pytest

from bark_pc.bark.links import LinkManager, share_payload
from bark_pc.core.config import (
    Settings,
    is_supabase_url,
    supabase_base_url,
    validate_server_url,
)
from bark_pc.core.errors import CourierRejected, LinkInvalid, StreamLost
from bark_pc.core.models import LinkState, PushStatus
from bark_pc.push.supabase_realtime import (
    SupabaseRealtimeReceiver,
    channel_topic,
    payload_from_broadcast,
    publish_bark_payload,
    realtime_ws_url,
    room_topic,
)
from bark_pc.push.supervisor import PushSupervisor

SUPABASE = "sb://demo-project.supabase.co"
KEY = "sb_publishable_abcdefghijklmnopqrstuvwxyz"
ROOM = "alerts"


# --------------------------------------------------------------------------- #
# URL + settings helpers
# --------------------------------------------------------------------------- #
def test_supabase_urls_are_normalized_and_local_projects_use_ws():
    assert validate_server_url("sb://demo-project.supabase.co") == SUPABASE
    assert validate_server_url("sb://https://demo-project.supabase.co/") == SUPABASE
    assert validate_server_url("sb://demo-project.supabase.co:8443/") == (
        "sb://demo-project.supabase.co:8443"
    )
    assert is_supabase_url(SUPABASE) is True
    assert is_supabase_url("https://api.day.app") is False

    assert supabase_base_url(SUPABASE) == "https://demo-project.supabase.co"
    assert supabase_base_url("sb://127.0.0.1:54321") == "http://127.0.0.1:54321"

    assert realtime_ws_url(SUPABASE, KEY).startswith(
        "wss://demo-project.supabase.co/realtime/v1/websocket?"
    )
    assert "apikey=sb_publishable_abcdefghijklmnopqrstuvwxyz" in realtime_ws_url(SUPABASE, KEY)
    assert "vsn=1.0.0" in realtime_ws_url(SUPABASE, KEY)
    assert realtime_ws_url("sb://127.0.0.1:54321", KEY).startswith(
        "ws://127.0.0.1:54321/realtime/v1/websocket?"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "sb://",
        "sb://user:password@project.supabase.co",
        "sb://project.supabase.co?x=1",
        "sb://project.supabase.co#fragment",
        "sb://project.supabase.co:notaport",
    ],
)
def test_invalid_supabase_urls_are_rejected(bad):
    with pytest.raises(ValueError):
        validate_server_url(bad)


def test_supabase_key_is_a_validated_setting():
    settings = Settings.from_dict({"supabase_key": "  " + KEY + "  "})
    assert settings.supabase_key == KEY
    assert settings.warnings == []

    bad = Settings.from_dict({"supabase_key": "two words"})
    assert bad.supabase_key == ""
    assert bad.warnings


# --------------------------------------------------------------------------- #
# Broadcast → Bark payload mapping
# --------------------------------------------------------------------------- #
def test_room_and_phoenix_topics_match_the_companion_hook():
    assert room_topic(ROOM) == "room:alerts:messages"
    assert channel_topic(ROOM) == "realtime:room:alerts:messages"


def test_channel_message_content_can_be_a_full_bark_payload():
    frame = {
        "event": "message",
        "payload": {
            "id": "sender-id",
            "type": "message",
            "content": {
                "aps": {"alert": {"title": "Deploy done", "body": "build 412"}},
                "group": "ops",
            },
        },
    }
    bark = json.loads(payload_from_broadcast(frame["event"], frame["payload"]))
    assert bark["aps"]["alert"] == {"title": "Deploy done", "body": "build 412"}
    assert bark["group"] == "ops"
    assert bark["id"] == "sender-id"


def test_channel_message_content_can_be_a_string_or_plain_title_body():
    as_text = payload_from_broadcast("message", {"id": "x", "content": "hello"})
    assert json.loads(as_text) == {"aps": {"alert": {"body": "hello"}}, "id": "x"}

    as_title_body = payload_from_broadcast(
        "message", {"id": "y", "content": {"title": "T", "body": "B"}}
    )
    assert json.loads(as_title_body)["aps"]["alert"] == {"title": "T", "body": "B"}


def test_a_string_content_is_text_even_when_it_looks_like_json():
    # Our own frames carry the body text, so a string is never decoded.
    content = json.dumps({"aps": {"alert": {"title": "T", "body": "B"}}})
    bark = json.loads(
        payload_from_broadcast("message", {"id": "j", "sender": "Ana", "content": content})
    )
    assert bark["aps"]["alert"] == {"body": content, "title": "Ana"}


def test_supabase_broadcast_envelope_and_direct_payloads_are_supported():
    envelope = {
        "event": "broadcast",
        "payload": {
            "type": "broadcast",
            "event": "message",
            "payload": {"id": "e", "content": {"title": "T", "body": "B"}},
        },
    }
    assert json.loads(payload_from_broadcast(envelope["event"], envelope["payload"]))[
        "aps"
    ]["alert"] == {"title": "T", "body": "B"}

    direct = {"aps": {"alert": {"title": "T", "body": "B"}}, "id": "d"}
    assert json.loads(payload_from_broadcast("message", direct))["id"] == "d"


def test_channel_message_sender_becomes_the_title_only_without_one():
    without_title = {"id": "s", "sender": "iPhone · Ana", "content": {"body": "B"}}
    bark = json.loads(payload_from_broadcast("message", without_title))
    assert bark["aps"]["alert"] == {"body": "B", "title": "iPhone · Ana"}
    assert bark["sender"] == "iPhone · Ana"

    with_title = {
        "id": "s",
        "sender": "iPhone · Ana",
        "content": {"type": "note", "title": "Deploy done", "body": "B"},
    }
    bark = json.loads(payload_from_broadcast("message", with_title))
    assert bark["aps"]["alert"] == {"title": "Deploy done", "body": "B"}

    string_content = {"id": "s", "sender": "Ana", "content": {"aps": {"alert": "raw"}}}
    bark = json.loads(payload_from_broadcast("message", string_content))
    assert bark["aps"]["alert"] == "raw"


def test_room_name_becomes_the_group_when_the_payload_has_none():
    bark = json.loads(
        payload_from_broadcast(
            "message", {"id": "r", "content": {"title": "T", "body": "B"}}, room=ROOM
        )
    )
    assert bark["group"] == ROOM

    explicit = json.loads(
        payload_from_broadcast(
            "message", {"content": {"body": "B", "group": "ops"}}, room=ROOM
        )
    )
    assert explicit["group"] == "ops"

    no_room = json.loads(payload_from_broadcast("message", {"content": {"body": "B"}}))
    assert "group" not in no_room

    recall = json.loads(
        payload_from_broadcast("message", {"content": {"delete": "1", "id": "m"}}, room=ROOM)
    )
    assert "group" not in recall


def test_control_and_command_events_are_not_notifications():
    assert payload_from_broadcast("command", {"content": {"method": "ping"}}) is None
    assert payload_from_broadcast("presence_diff", {"content": "x"}) is None
    assert payload_from_broadcast("phx_reply", {"status": "ok"}) is None
    assert payload_from_broadcast("message", {"type": "command", "content": "x"}) is None


# --------------------------------------------------------------------------- #
# Receiver protocol
# --------------------------------------------------------------------------- #
class FakeWebSocket:
    """Answers `phx_join`, then emits the supplied broadcast frames."""

    def __init__(self, frames: list[dict], *, join_status: str = "ok"):
        self.frames = list(frames)
        self.join_status = join_status
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, data):
        message = json.loads(data)
        self.sent.append(message)
        if message.get("event") == "phx_join":
            self.frames.insert(
                0,
                {
                    "event": "phx_reply",
                    "ref": message.get("ref"),
                    "payload": {"status": self.join_status, "response": {"reason": "denied"}},
                },
            )

    async def recv(self):
        if self.frames:
            return json.dumps(self.frames.pop(0))
        await anyio.sleep_forever()

    async def close(self):
        self.closed = True


def connect_to(ws):
    def connect(_url, **_kwargs):
        class Connection:
            async def __aenter__(self):
                return ws

            async def __aexit__(self, *_exc):
                return False

        return Connection()

    return connect


def test_receiver_joins_the_room_and_forwards_broadcasts():
    frames = [
        {
            "event": "message",
            "payload": {"id": "one", "content": {"aps": {"alert": {"title": "T", "body": "B"}}}},
        },
        {
            "event": "broadcast",
            "payload": {
                "type": "broadcast",
                "event": "message",
                "payload": {"id": "two", "content": "second"},
            },
        },
    ]
    ws = FakeWebSocket(frames)
    received: list[bytes] = []
    statuses: list[PushStatus] = []

    async def scenario():
        receiver = SupabaseRealtimeReceiver(
            server_url=SUPABASE,
            auth_key=KEY,
            room_name=ROOM,
            connect=connect_to(ws),
            heartbeat_interval=1000,
        )

        async def run():
            await receiver.run(
                topic=ROOM,
                on_message=received.append,
                on_status=lambda status, _reason: statuses.append(status),
            )

        with anyio.move_on_after(0.5):
            await run()

    anyio.run(scenario)

    join = ws.sent[0]
    assert join["event"] == "phx_join"
    assert join["topic"] == "realtime:room:alerts:messages"
    assert join["payload"]["config"]["broadcast"]["self"] is False
    assert statuses == [PushStatus.CONNECTING, PushStatus.ACTIVE]
    assert [json.loads(item)["id"] for item in received] == ["one", "two"]
    assert [json.loads(item)["group"] for item in received] == [ROOM, ROOM]


def test_receiver_surfaces_a_refused_subscription_as_a_bark_error():
    ws = FakeWebSocket([], join_status="error")

    async def scenario():
        receiver = SupabaseRealtimeReceiver(
            server_url=SUPABASE,
            auth_key=KEY,
            room_name=ROOM,
            connect=connect_to(ws),
            heartbeat_interval=1000,
        )
        await receiver.run(topic=ROOM, on_message=lambda _payload: None)

    with pytest.raises(CourierRejected):
        anyio.run(scenario)


def test_publish_uses_the_supabase_broadcast_envelope():
    ws = FakeWebSocket([])
    receiver = SupabaseRealtimeReceiver(
        server_url=SUPABASE,
        auth_key=KEY,
        room_name=ROOM,
        connect=connect_to(ws),
        publish_settle=0,
    )
    payload = {"aps": {"alert": {"title": "T", "body": "B"}}, "id": "self-test"}

    anyio.run(receiver.publish, payload)

    broadcast = ws.sent[-1]
    assert broadcast["event"] == "broadcast"
    assert broadcast["topic"] == "realtime:room:alerts:messages"
    assert broadcast["payload"]["type"] == "broadcast"
    assert broadcast["payload"]["event"] == "message"
    message = broadcast["payload"]["payload"]
    # `content` is the alert body as a plain string, not the push envelope.
    assert isinstance(message["content"], str)
    assert message["content"] == "B"
    assert message["type"] == "message"
    assert message["sender"] == "Bark Desktop"
    assert isinstance(message["id"], str) and message["id"]
    assert message["timestamp"].endswith("+00:00")


def test_published_sender_becomes_the_title_on_the_receiving_side():
    ws = FakeWebSocket([])
    receiver = SupabaseRealtimeReceiver(
        server_url=SUPABASE,
        auth_key=KEY,
        room_name=ROOM,
        connect=connect_to(ws),
        publish_settle=0,
    )
    # The compose bar's payload shape: the body only.
    payload = {
        "aps": {"alert": {"body": "Test notification"}},
        "id": "self-test-id",
    }

    anyio.run(receiver.publish, payload)

    message = ws.sent[-1]["payload"]["payload"]
    assert message["content"] == "Test notification"
    bark = json.loads(payload_from_broadcast("message", message, room=ROOM))
    assert bark["aps"]["alert"] == {
        "body": "Test notification",
        "title": "Bark Desktop",
    }
    assert bark["id"] == message["id"]
    assert bark["group"] == ROOM


def test_publish_accepts_a_custom_sender():
    ws = FakeWebSocket([])
    receiver = SupabaseRealtimeReceiver(
        server_url=SUPABASE,
        auth_key=KEY,
        room_name=ROOM,
        connect=connect_to(ws),
        publish_settle=0,
    )

    async def scenario():
        await receiver.publish({"aps": {"alert": {"body": "B"}}}, sender="Ana · desk")

    anyio.run(scenario)

    message = ws.sent[-1]["payload"]["payload"]
    assert message["sender"] == "Ana · desk"
    bark = json.loads(payload_from_broadcast("message", message))
    assert bark["aps"]["alert"] == {"body": "B", "title": "Ana · desk"}


def test_publish_helper_sends_one_broadcast():
    ws = FakeWebSocket([])
    payload = {"aps": {"alert": {"body": "B"}}, "id": "helper"}

    publish_bark_payload(
        SUPABASE, KEY, ROOM, payload, connect=connect_to(ws), timeout=1
    )

    assert ws.sent[-1]["event"] == "broadcast"
    assert ws.sent[-1]["payload"]["payload"]["content"] == "B"


def test_publish_sends_only_the_alert_text():
    ws = FakeWebSocket([])
    receiver = SupabaseRealtimeReceiver(
        server_url=SUPABASE,
        auth_key=KEY,
        room_name=ROOM,
        connect=connect_to(ws),
        publish_settle=0,
    )

    # Extras on the payload (group, id, aps bookkeeping) stay off the wire.
    anyio.run(
        receiver.publish,
        {
            "aps": {"alert": {"title": "Deploy done", "body": "build 412"}},
            "group": "ops",
            "id": "build-412",
        },
    )
    assert ws.sent[-1]["payload"]["payload"]["content"] == "build 412"

    # A title-only payload still carries something readable.
    anyio.run(receiver.publish, {"aps": {"alert": {"title": "Only a title"}}})
    assert ws.sent[-1]["payload"]["payload"]["content"] == "Only a title"

    # No alert text at all (for example a recall) is not broadcast as an empty
    # message; the room carries messages, not deletes.
    sent = len(ws.sent)
    anyio.run(receiver.publish, {"delete": "1", "id": "m"})
    assert len(ws.sent) == sent


class NoMaterialReceiver:
    needs_material = False

    def __init__(self):
        self.material = "unset"

    async def run(self, *, material, topic, on_message, on_status, on_token=None, on_activity=None):
        self.material = material
        on_status(PushStatus.ACTIVE, None)
        raise StreamLost("closed")


def test_supervisor_does_not_require_material_for_supabase():
    receiver = NoMaterialReceiver()
    statuses: list[PushStatus] = []

    def material_getter():  # pragma: no cover - must never be called
        raise AssertionError("material must not be requested for a Supabase receiver")

    supervisor = PushSupervisor(
        receiver,
        material_getter=material_getter,
        topic=ROOM,
        on_message=lambda _payload: None,
        on_status=lambda status, _reason: statuses.append(status),
    )
    anyio.run(supervisor.run_loop, 1)

    assert receiver.material is None
    assert PushStatus.DEGRADED in statuses


# --------------------------------------------------------------------------- #
# Link lifecycle
# --------------------------------------------------------------------------- #
def test_supabase_registration_skips_http_and_verifies_locally(db):
    def client_factory(_url):  # pragma: no cover - must never be called
        raise AssertionError("Supabase registration must not use the Bark HTTP client")

    manager = LinkManager(db, client_factory=client_factory)
    link = manager.register_supabase(SUPABASE, KEY, push_topic=ROOM)

    assert link.state == LinkState.LINKED
    assert link.is_supabase is True
    assert link.device_key == KEY
    assert link.push_topic == ROOM
    assert db.get_link().device_key == KEY
    assert manager.verify() is True
    assert share_payload(link) == f"{SUPABASE}/{KEY}"


def test_supabase_registration_requires_the_short_form_and_a_key(db):
    manager = LinkManager(db, client_factory=lambda _url: None)
    with pytest.raises(LinkInvalid):
        manager.register_supabase("https://api.day.app", KEY)
    with pytest.raises(LinkInvalid):
        manager.register_supabase(SUPABASE, "  ")


def test_own_broadcast_is_flagged_as_self_and_others_are_not():
    own = payload_from_broadcast(
        "message", {"id": "mine", "sender": "Bark Desktop", "content": "hi"}
    )
    assert json.loads(own)["bark_desktop_self"] == "1"

    other = payload_from_broadcast(
        "message", {"id": "theirs", "sender": "Ana", "content": "hi"}
    )
    assert "bark_desktop_self" not in json.loads(other)
