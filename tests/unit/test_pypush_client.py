"""pypush adapter: activation mapping, streaming/ack contract, error taxonomy.

`pypush.apns` is always replaced with fakes, so no test contacts
`albert.apple.com` or a courier (see
`specs/001-bark-desktop-client/contracts/pypush-adapter.md`, "Test seam").
"""

from __future__ import annotations

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from pypush.apns import filters, protocol

from bark_pc.core.errors import (
    ActivationRejected,
    ActivationUnavailable,
    BarkError,
    CourierRejected,
    StreamLost,
    TokenRejected,
)
from bark_pc.core.models import PushStatus
from bark_pc.push import pypush_client
from bark_pc.push.pypush_client import PypushReceiver, cert_fingerprint

from ..conftest import make_activation_material

MATERIAL = make_activation_material()
TOKEN = b"\x01" * 32
SCOPED = b"\x02" * 32
TOPIC = "me.fin.bark"


# --------------------------------------------------------------------------- #
# Fakes for the two `pypush.apns` entry points the adapter uses
# --------------------------------------------------------------------------- #


class FakeCommand:
    """Stand-in for non-notification courier traffic (keepalive acks)."""

    def __init__(self, payload: bytes):
        self.payload = payload


def make_message(
    payload: bytes,
    *,
    topic: str = TOPIC,
    token: bytes = TOKEN,
) -> protocol.SendMessageCommand:
    """A real inbound `SendMessageCommand`, exactly as the courier decodes one."""
    return protocol.SendMessageCommand(
        payload=payload, id=b"\x01\x02\x03\x04", topic=topic, token=token, outgoing=False
    )


class FakeConnection:
    """Mimics the slice of `apns.APNsConnection` the adapter touches."""

    def __init__(
        self,
        *,
        token: bytes = TOKEN,
        token_error: BaseException | None = None,
        payloads: tuple[bytes, ...] = (),
        messages: tuple | None = None,
        scoped: bytes = SCOPED,
        stream_error: BaseException | None = None,
        block: bool = False,
        activity: tuple[bytes, ...] = (),
    ):
        self._token = token
        self.token_error = token_error
        # A Bark server pushes to the device's *base* token, not to the token
        # minted for this connection, so that is the default here.
        self.messages = (
            list(messages)
            if messages is not None
            else [make_message(payload, token=token) for payload in payloads]
        )
        self.payloads = [message.payload for message in self.messages]
        self.scoped = scoped
        self.stream_error = stream_error
        self.block = block
        self.activity = list(activity)
        self.activity_streams = 0
        self.streams: list[bool] = []
        self.filters: list = []
        self.topics: list[str] = []
        self.subscribed_topics: list[str] = []
        self.acked: list = []

    @property
    def base_token(self):
        async def getter() -> bytes:
            if self.token_error is not None:
                raise self.token_error
            return self._token

        return getter()

    async def mint_scoped_token(self, topic: str) -> bytes:
        self.topics.append(topic)
        return self.scoped

    def _filter(self, topics: list[str]):
        """The topic subscription `notification_stream` would install."""
        self.subscribed_topics.extend(topics)

        @asynccontextmanager
        async def filtered():
            yield

        return filtered()

    async def _commands(self, filter=None):
        """Raw broadcast traffic, filtered the way `FilteredStream` does."""
        for command in [*(FakeCommand(payload) for payload in self.activity), *self.messages]:
            if filter is None or filter(command) is not None:
                yield command
        if self.stream_error is not None:
            raise self.stream_error
        if self.block:
            await anyio.sleep_forever()

    def _receive_stream(self, filter=None, backlog: bool = True):
        """The broadcast stream: applied filters, as `pypush` does."""

        @asynccontextmanager
        async def stream():
            self.filters.append(filter)
            if filter is filters.ALL:
                self.activity_streams += 1
            else:
                self.streams.append(backlog)
            yield self._commands(filter)

        return stream()

    async def ack(self, command) -> None:
        self.acked.append(command)


def real_material_pair():
    """The (certificate, private key) pair that `apns.activate` really returns."""
    reference = make_activation_material()
    certificate = x509.load_pem_x509_certificate(reference.cert_pem.encode("ascii"))
    private_key = serialization.load_pem_private_key(
        reference.key_pem.encode("ascii"), password=None
    )
    return certificate, private_key


def install(monkeypatch, *, connection=None, activate_error=None):
    """Replace `apns.activate` / `apns.create_apns_connection` with fakes."""
    connection = connection or FakeConnection()
    calls: dict[str, object] = {}

    async def fake_activate(**kwargs):
        calls["activate"] = kwargs
        if activate_error is not None:
            raise activate_error
        calls["material"] = real_material_pair()
        return calls["material"]

    def fake_create_connection(certificate, private_key, **kwargs):
        calls["connection"] = {"certificate": certificate, "private_key": private_key, **kwargs}

        @asynccontextmanager
        async def context():
            yield connection

        return context()

    monkeypatch.setattr(pypush_client.apns, "activate", fake_activate)
    monkeypatch.setattr(pypush_client.apns, "create_apns_connection", fake_create_connection)
    return calls, connection


# --------------------------------------------------------------------------- #
# Activation
# --------------------------------------------------------------------------- #


def activate(monkeypatch, *, activate_error=None):
    calls, _ = install(monkeypatch, activate_error=activate_error)
    receiver = PypushReceiver()
    material = anyio.run(lambda: receiver.activate(udid=MATERIAL.udid, serial=MATERIAL.serial))
    return calls, material


def test_activate_returns_pem_material_without_a_base_token(monkeypatch):
    calls, material = activate(monkeypatch)

    kwargs = dict(calls["activate"])
    client = kwargs.pop("http_client")
    assert isinstance(client, httpx.AsyncClient)
    assert kwargs == {
        "device_class": "Windows",
        "udid": MATERIAL.udid,
        "serial": MATERIAL.serial,
    }
    assert (material.udid, material.serial) == (MATERIAL.udid, MATERIAL.serial)
    # The base token only exists after the first ConnectAck.
    assert material.base_token_hex is None
    certificate, private_key = calls["material"]
    parsed = x509.load_pem_x509_certificate(material.cert_pem.encode("ascii"))
    assert cert_fingerprint(parsed) == cert_fingerprint(certificate)
    loaded_key = serialization.load_pem_private_key(material.key_pem.encode("ascii"), password=None)
    assert loaded_key.private_numbers() == private_key.private_numbers()


def test_activate_never_opens_a_courier_connection(monkeypatch):
    calls, _ = activate(monkeypatch)

    assert "connection" not in calls


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.TimeoutException("timed out"), ActivationUnavailable),
        (httpx.ConnectError("dns failure"), ActivationUnavailable),
        (
            Exception(
                "Failed to get certificate from Albert: There is a problem with your device."
            ),
            ActivationRejected,
        ),
        (Exception("something unexpected"), ActivationUnavailable),
    ],
)
def test_activate_maps_library_failures_onto_the_error_taxonomy(monkeypatch, error, expected):
    with pytest.raises(expected) as caught:
        activate(monkeypatch, activate_error=error)

    assert isinstance(caught.value, BarkError)
    # The upstream text is preserved so the log explains the rejection.
    assert str(error) in str(caught.value)


def test_activation_error_is_the_historic_alias_for_activation_rejected():
    assert issubclass(pypush_client.ActivationError, ActivationRejected)


# --------------------------------------------------------------------------- #
# Activation logging
# --------------------------------------------------------------------------- #


def test_activate_uses_an_instrumented_http_client(monkeypatch):
    calls, _ = activate(monkeypatch)
    client = calls["activate"]["http_client"]

    assert pypush_client._log_activation_request in client.event_hooks["request"]
    assert pypush_client._log_activation_response in client.event_hooks["response"]


def test_activate_logs_the_attempt_and_the_success(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="bark_pc.pypush"):
        activate(monkeypatch)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "activation: requesting anonymous device certificate" in message
        and MATERIAL.udid in message
        for message in messages
    )
    assert any("activation: succeeded in" in message for message in messages)
    # Certificates and keys never reach the log.
    assert "BEGIN" not in caplog.text


def test_activate_logs_apple_reason_and_keeps_the_traceback_at_debug(monkeypatch, caplog):
    error = Exception("Failed to get certificate from Albert: There is a problem with your device.")

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        with pytest.raises(ActivationRejected):
            activate(monkeypatch, activate_error=error)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("activation: rejected after" in record.getMessage() for record in warnings)
    assert "There is a problem with your device." in caplog.text
    assert any(
        record.getMessage() == "activation failure details" and record.exc_info
        for record in caplog.records
    )


def make_activation_response(status: int, body: str, *, read: bool = False) -> httpx.Response:
    request = httpx.Request(
        "POST", "https://albert.apple.com/deviceservices/deviceActivation?device=Windows"
    )
    if read:
        return httpx.Response(status, text=body, request=request)
    # Real responses reach the hook unread: `response.text` raises at that point.
    return httpx.Response(status, stream=httpx.ByteStream(body.encode("utf-8")), request=request)


def test_response_hook_logs_the_endpoint_status_and_apple_error_text(caplog):
    body = "<html><body><b>There is a problem with your device.</b></body></html>"

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, make_activation_response(200, body))

    assert "activation response: HTTP 200" in caplog.text
    assert "Apple rejected the activation request (HTTP 200)" in caplog.text
    warning = next(
        record.getMessage() for record in caplog.records if "Apple rejected" in record.getMessage()
    )
    # The user-facing warning carries Apple's reason only; markup stays in debug.
    assert warning == (
        "Apple rejected the activation request (HTTP 200): There is a problem with your device."
    )


def test_response_hook_reads_an_unread_body_and_leaves_it_for_the_adapter(caplog):
    response = make_activation_response(200, "<Protocol>ok</Protocol>")
    assert not hasattr(response, "_content")

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, response)

    # `apns.activate()` parses `response.text` after the hooks have run.
    assert response.text == "<Protocol>ok</Protocol>"


def test_response_hook_handles_a_body_that_was_already_read(caplog):
    response = make_activation_response(
        200, "<html><b>There is a problem with your device.</b></html>", read=True
    )

    with caplog.at_level(logging.WARNING, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, response)

    assert "There is a problem with your device." in caplog.text


# The exact shape Apple returns when it refuses activation (see the log from a
# real run): a wizard page whose <Protocol> block holds an empty <dict/>.
APPLE_REJECTION_BODY = """
<Document disableHistory="true">
    <Protocol><plist version="1.0">
  <dict/>
</plist></Protocol>
    <ScrollView>
        <TextView>
            <b>There is a problem with your device.</b>
        </TextView>
    </ScrollView>
</Document>
"""


def test_response_hook_flags_an_empty_activation_plist(caplog):
    response = make_activation_response(200, APPLE_REJECTION_BODY, read=True)

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, response)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("Apple rejected the activation request (HTTP 200): There is a problem")
        for message in messages
    )
    assert any("without a device certificate" in message for message in messages)


def test_response_hook_is_quiet_for_a_successful_activation(caplog):
    body = "<Protocol><dict><key>device-activation</key></dict></Protocol>"

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, make_activation_response(200, body))

    assert "Apple rejected" not in caplog.text
    assert "activation error body" not in caplog.text


def test_response_hook_bounds_and_summarizes_unknown_bodies(caplog):
    body = "<html>" + "x" * 5000 + "</html>"

    with caplog.at_level(logging.WARNING, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_response, make_activation_response(503, body))

    rejection = next(
        record.getMessage() for record in caplog.records if "Apple rejected" in record.getMessage()
    )
    assert rejection.endswith("…")
    assert len(rejection) < 500


def test_request_hook_logs_the_endpoint_not_the_payload(caplog):
    request = httpx.Request(
        "POST",
        "https://albert.apple.com/deviceservices/deviceActivation?device=Windows",
        data={"activation-info": "secret-plist"},
    )

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        anyio.run(pypush_client._log_activation_request, request)

    assert (
        "activation request: POST https://albert.apple.com/deviceservices/deviceActivation"
        in caplog.text
    )
    assert "device=Windows" in caplog.text
    assert "secret-plist" not in caplog.text


# --------------------------------------------------------------------------- #
# Streaming
# --------------------------------------------------------------------------- #


def stream(
    monkeypatch,
    *,
    connection=None,
    material: object = MATERIAL,
    on_message=None,
    on_token=None,
    on_activity=None,
):
    connection = connection or FakeConnection()
    calls, connection = install(monkeypatch, connection=connection)
    receiver = PypushReceiver()
    statuses: list[tuple[PushStatus, str | None]] = []

    async def scenario():
        await receiver.run(
            material=material,
            topic=TOPIC,
            on_message=on_message or (lambda payload: None),
            on_status=lambda status, reason: statuses.append((status, reason)),
            on_token=on_token,
            on_activity=on_activity,
        )

    anyio.run(scenario)
    return calls, connection, statuses


def test_run_streams_and_acks_every_payload(monkeypatch):
    payloads = (b'{"a": 1}', b'{"a": 2}', b'{"a": 3}')
    seen: list[bytes] = []
    tokens: list[str] = []

    calls, connection, statuses = stream(
        monkeypatch,
        connection=FakeConnection(payloads=payloads),
        on_message=seen.append,
        on_token=tokens.append,
    )

    assert seen == list(payloads)
    assert [command.payload for command in connection.acked] == list(payloads)
    assert connection.topics == [TOPIC]
    # The cached base token is replayed to the courier and reported upstream.
    assert calls["connection"]["token"] == bytes.fromhex(MATERIAL.base_token_hex)
    assert calls["connection"]["sandbox"] is False
    assert calls["connection"]["courier"] == pypush_client.COURIER
    assert tokens == [TOKEN.hex()]
    assert statuses[0] == (PushStatus.CONNECTING, None)
    assert (PushStatus.ACTIVE, None) in statuses
    # The topic is subscribed; the notification stream keeps `pypush`'s backlog
    # so a push that lands between the filter update and the stream open is not
    # lost.
    assert connection.subscribed_topics == [TOPIC]
    assert connection.streams == [True]


def test_run_delivers_a_message_addressed_to_the_base_token(monkeypatch):
    """Regression: Bark pushes carry the base token, never the scoped token.

    `pypush`'s `notification_stream(topic, scoped)` filters
    `command.token == scoped`, so real pushes were broadcast by the courier and
    then dropped inside the library: the log showed
    `Broadcasting SendMessageCommand(... topic='me.fin.bark' ...)` while the app
    received nothing.
    """
    seen: list[bytes] = []
    connection = FakeConnection(
        messages=(make_message(b'{"a": 1}', token=TOKEN),), scoped=SCOPED
    )

    stream(monkeypatch, connection=connection, on_message=seen.append)

    assert connection._token != connection.scoped
    assert seen == [b'{"a": 1}']
    assert [command.payload for command in connection.acked] == [b'{"a": 1}']


def test_run_delivers_a_message_addressed_to_the_scoped_token(monkeypatch):
    seen: list[bytes] = []
    connection = FakeConnection(
        messages=(make_message(b'scoped', token=SCOPED),), scoped=SCOPED
    )

    stream(monkeypatch, connection=connection, on_message=seen.append)

    assert seen == [b"scoped"]


def test_run_ignores_messages_for_another_topic(monkeypatch):
    seen: list[bytes] = []
    connection = FakeConnection(
        messages=(
            make_message(b"other app", topic="com.example.other"),
            make_message(b"ours"),
        )
    )

    stream(monkeypatch, connection=connection, on_message=seen.append)

    assert seen == [b"ours"]
    # Only our topic is acknowledged: the other app's push is not addressed here.
    assert [command.payload for command in connection.acked] == [b"ours"]


def test_run_logs_an_unexpected_token_but_still_delivers(monkeypatch, caplog):
    seen: list[bytes] = []
    connection = FakeConnection(messages=(make_message(b"unknown", token=b"\x03" * 32),))

    with caplog.at_level(logging.DEBUG, logger="bark_pc.pypush"):
        stream(monkeypatch, connection=connection, on_message=seen.append)

    assert seen == [b"unknown"]
    assert any(
        "carries an unexpected token" in record.getMessage() for record in caplog.records
    )


def test_message_filter_matches_a_topic_that_decodes_as_a_hash():
    # A topic unknown to `pypush` stays a raw SHA-1 digest in the command.
    command = make_message(b"payload")
    command.topic = hashlib.sha1(TOPIC.encode()).digest()

    assert pypush_client.message_filter(TOPIC)(command) is command
    assert pypush_client.message_filter("other.topic")(command) is None


def test_message_filter_ignores_non_notification_commands():
    matches = pypush_client.message_filter(TOPIC, base_token=TOKEN)

    assert matches(FakeCommand(b"keepalive-ack")) is None


def test_run_connects_without_a_cached_token_when_material_has_none(monkeypatch):
    calls, _, _ = stream(monkeypatch, material=make_activation_material(token=None))

    assert calls["connection"]["token"] is None


def test_ack_is_sent_even_when_the_payload_callback_raises(monkeypatch):
    payloads = (b"first", b"second")

    def explode(payload: bytes) -> None:
        raise ValueError(f"cannot handle {payload!r}")

    _, connection, statuses = stream(
        monkeypatch, connection=FakeConnection(payloads=payloads), on_message=explode
    )

    # A callback bug must never drop an ack or kill the stream (invariant 2).
    assert [command.payload for command in connection.acked] == list(payloads)
    assert (PushStatus.ACTIVE, None) in statuses


def test_token_callback_failure_does_not_break_the_stream(monkeypatch):
    def explode(token: str) -> None:
        raise RuntimeError("callback bug")

    _, connection, statuses = stream(
        monkeypatch,
        connection=FakeConnection(payloads=(b"payload",)),
        on_token=explode,
    )

    assert [command.payload for command in connection.acked] == [b"payload"]
    assert (PushStatus.ACTIVE, None) in statuses


def test_inbound_courier_commands_are_reported_as_activity(monkeypatch):
    pings: list[int] = []
    enough = anyio.Event()
    connection = FakeConnection(activity=(b"keepalive-ack", b"keepalive-ack"), block=True)

    def on_activity() -> None:
        pings.append(1)
        if len(pings) == 2:
            enough.set()

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(
                lambda: PypushReceiver().run(
                    material=MATERIAL,
                    topic=TOPIC,
                    on_message=lambda payload: None,
                    on_activity=on_activity,
                )
            )
            await enough.wait()
            group.cancel_scope.cancel()

    install(monkeypatch, connection=connection)
    anyio.run(scenario)

    assert len(pings) == 2
    assert connection.activity_streams == 1
    # Keepalive traffic is liveness only, never a notification.
    assert connection.acked == []


def test_no_activity_stream_is_opened_without_a_callback(monkeypatch):
    connection = FakeConnection(activity=(b"keepalive-ack",))

    _, connection, _ = stream(monkeypatch, connection=connection)

    assert connection.activity_streams == 0


def test_activity_callback_failures_do_not_break_the_stream(monkeypatch):
    received: list[bytes] = []
    notified = anyio.Event()
    connection = FakeConnection(payloads=(b"payload",), activity=(b"ack",), block=True)

    def on_message(payload: bytes) -> None:
        received.append(payload)
        notified.set()

    def on_activity() -> None:
        raise RuntimeError("callback bug")

    async def scenario():
        async with anyio.create_task_group() as group:
            group.start_soon(
                lambda: PypushReceiver().run(
                    material=MATERIAL,
                    topic=TOPIC,
                    on_message=on_message,
                    on_activity=on_activity,
                )
            )
            await notified.wait()
            await anyio.sleep(0)
            group.cancel_scope.cancel()

    install(monkeypatch, connection=connection)
    anyio.run(scenario)

    assert received == [b"payload"]
    assert [command.payload for command in connection.acked] == [b"payload"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("courier closed"), StreamLost),
        (httpx.ConnectError("network down"), ActivationUnavailable),
        (TokenRejected("token rejected"), TokenRejected),
    ],
)
def test_run_maps_stream_failures_and_terminates(monkeypatch, error, expected):
    with pytest.raises(expected):
        stream(monkeypatch, connection=FakeConnection(stream_error=error))


def test_run_propagates_cancellation_instead_of_mapping_it(monkeypatch):
    install(monkeypatch, connection=FakeConnection(block=True))
    receiver = PypushReceiver()
    active = anyio.Event()
    escaped: list[BaseException] = []

    async def scenario():
        async with anyio.create_task_group() as group:

            async def stream_forever():
                await receiver.run(
                    material=MATERIAL,
                    topic=TOPIC,
                    on_message=lambda payload: None,
                    on_status=lambda status, reason: (
                        active.set() if status is PushStatus.ACTIVE else None
                    ),
                )

            group.start_soon(stream_forever)
            await active.wait()
            group.cancel_scope.cancel()

    try:
        anyio.run(scenario)
    except BaseException as exc:  # pragma: no cover - failure path
        escaped.append(exc)
        raise

    assert escaped == []


# --------------------------------------------------------------------------- #
# Token fetch, error taxonomy, stop
# --------------------------------------------------------------------------- #


def test_fetch_base_token_returns_hex(monkeypatch):
    install(monkeypatch)

    token = anyio.run(lambda: PypushReceiver().fetch_base_token(MATERIAL))

    assert token == TOKEN.hex()


def test_fetch_base_token_maps_failures(monkeypatch):
    install(monkeypatch, connection=FakeConnection(token_error=RuntimeError("token invalid")))

    with pytest.raises(TokenRejected):
        anyio.run(lambda: PypushReceiver().fetch_base_token(MATERIAL))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (StreamLost("already mapped"), StreamLost),
        (CourierRejected("status 6"), CourierRejected),
        (TokenRejected("token rejected"), TokenRejected),
        (ActivationRejected("rejected"), ActivationRejected),
        (httpx.ReadTimeout("slow"), ActivationUnavailable),
        (AssertionError("bad ack"), CourierRejected),
        (RuntimeError("token invalid"), TokenRejected),
        (ValueError("boom"), StreamLost),
    ],
)
def test_map_error_taxonomy(error, expected):
    assert isinstance(PypushReceiver()._map_error(error), expected)


def test_stop_is_silent_and_repeatable(monkeypatch):
    install(monkeypatch)
    receiver = PypushReceiver()

    anyio.run(receiver.stop)
    anyio.run(receiver.stop)


# --------------------------------------------------------------------------- #
# Layering
# --------------------------------------------------------------------------- #


def test_pypush_is_imported_only_by_the_adapter():
    root = Path(pypush_client.__file__).resolve().parents[2]
    pattern = re.compile(r"^\s*(?:from|import)\s+pypush\b", re.MULTILINE)
    offenders = [
        str(path.relative_to(root))
        for path in (root / "bark_pc").rglob("*.py")
        if path.name != "pypush_client.py" and pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []
