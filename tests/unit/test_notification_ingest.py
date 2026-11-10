"""Parse → store → present, plus the adapter's ack guarantee (temp SQLite only)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import anyio
import pytest
from pypush.apns import protocol

from bark_pc.core.models import PushStatus
from bark_pc.push.ingest import Ingestor
from bark_pc.push.payload import ParseOutcome
from bark_pc.push.pypush_client import PypushReceiver

from ..conftest import FakeNotifier, load_payload

SERVER = "api.day.app"


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def ingestor(db, notifier) -> Ingestor:
    return Ingestor(db, notifier=notifier)


def test_three_payloads_produce_three_rows_newest_first(db, ingestor, notifier):
    for name in ("text.json", "text_with_subtitle.json", "group.json"):
        result = ingestor.handle_payload(load_payload(name), source_server=SERVER)
        assert result.outcome == ParseOutcome.MESSAGE

    rows = db.list_notifications()
    assert len(rows) == 3
    assert [row.remote_id for row in rows] == ["group-1", "sub-1", "text-1"]
    assert len(notifier.shown) == 3
    assert db.unread_count() == 3


def test_duplicate_remote_id_is_ignored(db, ingestor, notifier):
    ingestor.handle_payload(load_payload("text.json"), source_server=SERVER)
    ingestor.handle_payload(load_payload("duplicate_id.json"), source_server=SERVER)

    rows = db.list_notifications()
    assert len(rows) == 1
    assert rows[0].body == "build 412 is live"  # the first delivery wins
    assert len(notifier.shown) == 2  # presentation still happens per push


def test_same_remote_id_on_a_different_server_is_a_new_row(db, ingestor):
    ingestor.handle_payload(load_payload("text.json"), source_server="api.day.app")
    ingestor.handle_payload(load_payload("text.json"), source_server="bark.example.com")
    assert db.count_notifications() == 2


def test_delete_outcome_removes_the_matching_row(db, ingestor):
    ingestor.handle_payload(load_payload("text.json"), source_server=SERVER)
    assert db.count_notifications() == 1

    recall = json.dumps({"aps": {"content-available": 1}, "delete": "1", "id": "text-1"}).encode()
    result = ingestor.handle_payload(recall, source_server=SERVER)

    assert result.outcome == ParseOutcome.DELETE
    assert result.deleted_remote_id == "text-1"
    assert db.count_notifications() == 0


def test_empty_and_malformed_payloads_are_not_stored(db, ingestor):
    ingestor.handle_payload(load_payload("empty_alert.json"), source_server=SERVER)
    ingestor.handle_payload(b"not json at all", source_server=SERVER)
    assert db.count_notifications() == 0


def test_payload_without_id_is_deduplicated_within_the_window(db, ingestor):
    raw = json.dumps({"aps": {"alert": {"title": "No id", "body": "once"}}}).encode()
    ingestor.handle_payload(raw, source_server=SERVER)
    ingestor.handle_payload(raw, source_server=SERVER)
    assert db.count_notifications() == 1


def test_notifier_failure_does_not_lose_the_row(db, ingestor, notifier):
    notifier.fail = True
    result = ingestor.handle_payload(load_payload("text.json"), source_server=SERVER)
    assert result.notification_id is not None
    assert db.count_notifications() == 1


# --------------------------------------------------------------------------- #
# Adapter: one ack per delivered message, even when the payload is dropped
# --------------------------------------------------------------------------- #


class _Awaitable:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def get():
            return self.value

        return get().__await__()


class _Stream:
    def __init__(self, commands):
        self._commands = commands

    def __aiter__(self):
        self._iterator = iter(self._commands)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration from None

    async def aclose(self):
        return None


class FakeConnection:
    def __init__(self, payloads, token_hex="ab" * 32):
        self.base_token = _Awaitable(bytes.fromhex(token_hex))
        self.acks: list[bytes] = []
        self._commands = [
            protocol.SendMessageCommand(
                payload=payload,
                id=bytes([index]),
                topic="me.fin.bark",
                token=b"scoped",
                outgoing=False,
            )
            for index, payload in enumerate(payloads)
        ]
        self.minted_topic = None

    async def mint_scoped_token(self, topic):
        self.minted_topic = topic
        return b"scoped"

    def _filter(self, topics):
        @asynccontextmanager
        async def filtered():
            yield

        return filtered()

    def _receive_stream(self, filter=None, backlog=True):
        @asynccontextmanager
        async def stream():
            selected = [c for c in self._commands if filter is None or filter(c) is not None]
            yield _Stream(selected)

        return stream()

    async def ack(self, command, status=0):
        self.acks.append(command.id)

    async def aclose(self):
        return None


def test_receiver_acks_every_message(monkeypatch, db, notifier):
    payloads = [
        load_payload("text.json"),
        b"{ not json",  # malformed: still acked
        load_payload("delete.json"),  # dropped outcome: still acked
        load_payload("text_with_subtitle.json"),
    ]
    connection = FakeConnection(payloads)

    @asynccontextmanager
    async def fake_connection(self, material):
        yield connection

    monkeypatch.setattr(PypushReceiver, "_connection", fake_connection)
    ingestor = Ingestor(db, notifier=notifier)
    statuses: list[PushStatus] = []
    tokens: list[str] = []

    async def run():
        receiver = PypushReceiver()
        await receiver.run(
            material=None,
            topic="me.fin.bark",
            on_message=lambda raw: ingestor.handle_payload(raw, source_server=SERVER),
            on_status=lambda status, reason: statuses.append(status),
            on_token=tokens.append,
        )

    anyio.run(run)

    assert len(connection.acks) == 4
    assert statuses == [PushStatus.CONNECTING, PushStatus.ACTIVE]
    assert tokens == ["ab" * 32]
    assert connection.minted_topic == "me.fin.bark"
    assert db.count_notifications() == 2  # text + subtitle; malformed/delete add nothing


# --------------------------------------------------------------------------- #
# Own messages (Realtime compose bar): stored read, never toasted or spoken
# --------------------------------------------------------------------------- #
def test_own_message_is_stored_read_and_not_presented(db, notifier):
    seen: list = []
    ingestor = Ingestor(db, notifier=notifier, on_stored=seen.append)
    raw = json.dumps(
        {
            "aps": {"alert": {"body": "hello from me"}},
            "id": "own-1",
            "bark_desktop_self": "1",
        }
    ).encode()

    result = ingestor.handle_payload(raw, source_server=SERVER)

    assert result.notification_id is not None
    stored = db.find_by_remote_id(SERVER, "own-1")
    assert stored is not None
    assert stored.read_at is not None  # stored read: no unread badge
    assert result.notification is not None and result.notification.from_self is True
    assert db.unread_count() == 0
    assert notifier.shown == []
    assert [n.remote_id for n in seen] == ["own-1"]


def test_from_self_argument_marks_a_payload_without_the_marker(db, notifier):
    ingestor = Ingestor(db, notifier=notifier)
    result = ingestor.handle_payload(
        load_payload("text.json"), source_server=SERVER, from_self=True
    )

    stored = db.find_by_remote_id(SERVER, "text-1")
    assert stored is not None and stored.read_at is not None
    assert result.notification is not None and result.notification.from_self is True
    assert notifier.shown == []
