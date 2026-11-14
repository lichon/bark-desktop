"""How the ANCS backend asks for an encrypted link.

Bumble is not importable in the test environment, so `_require_bumble` is
replaced with a stub and the connection is a fake that records the calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
import types

import pytest

from bark_pc.bluetooth import AncsError, ancs_backend

LOGGER = logging.getLogger("bark_pc.ancs.test")

HCI_STUB = types.SimpleNamespace(
    Role=types.SimpleNamespace(CENTRAL=0, PERIPHERAL=1),
)


class FailureReason:
    name = "UNSPECIFIED_REASON"


class FakeConnection:
    """Only the bits of `bumble.device.Connection` the backend touches."""

    EVENT_PAIRING_FAILURE = "pairing_failure"
    PEER = "AA:BB:CC:DD:EE:FF"

    def __init__(self, role, *, encrypted=False, refuse=False):
        self.role = role
        self.peer_address = self.PEER
        self.is_encrypted = encrypted
        self.refuse = refuse
        self.requested = 0
        self.paired = 0
        self._listeners: dict[str, list] = {}
        # Link-security facts the backend copies into `security_summary()`.
        self.authenticated = False
        self.sc = False
        self.encryption_key_size = 0
        self.role_name = "PERIPHERAL" if role == HCI_STUB.Role.PERIPHERAL else "CENTRAL"
        self.pairing_peer_authentication_requirements = None

    # -- events ------------------------------------------------------------ #
    def on(self, event, listener) -> None:
        self._listeners.setdefault(event, []).append(listener)

    def remove_listener(self, event, listener) -> None:
        self._listeners.get(event, []).remove(listener)

    def emit(self, event, *args) -> None:
        for listener in list(self._listeners.get(event, [])):
            listener(*args)

    # -- pairing ----------------------------------------------------------- #
    def request_pairing(self) -> None:
        self.requested += 1
        if self.refuse:
            loop = asyncio.get_running_loop()
            loop.call_soon(self.emit, self.EVENT_PAIRING_FAILURE, FailureReason())
        else:
            self.is_encrypted = True

    async def pair(self) -> None:
        self.paired += 1
        if self.refuse:
            raise AncsError("pairing refused")
        self.is_encrypted = True


@pytest.fixture
def backend(monkeypatch):
    def fake_require_bumble():
        return (None, None, HCI_STUB, None, None, None, None)

    monkeypatch.setattr(ancs_backend, "_require_bumble", fake_require_bumble)
    return ancs_backend.BumbleAncsBackend(log=LOGGER)


def wait_for_secure_link(backend, connection):
    return asyncio.run(backend._wait_for_secure_link(connection))


def test_peripheral_lets_the_central_start_the_pairing(backend):
    """An iPhone-initiated (central) pairing, not a peripheral Pairing Request."""
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL)

    wait_for_secure_link(backend, connection)

    assert connection.requested == 1
    assert connection.paired == 0
    assert connection.is_encrypted is True


def test_central_pairs_itself(backend):
    connection = FakeConnection(HCI_STUB.Role.CENTRAL)

    wait_for_secure_link(backend, connection)

    assert connection.paired == 1
    assert connection.requested == 0


def test_an_already_encrypted_link_is_left_alone(backend):
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL, encrypted=True)

    wait_for_secure_link(backend, connection)

    assert (connection.requested, connection.paired) == (0, 0)


def test_a_refusal_from_the_iphone_is_reported(backend):
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL, refuse=True)

    with pytest.raises(AncsError) as error:
        wait_for_secure_link(backend, connection)

    message = str(error.value)
    assert "UNSPECIFIED_REASON" in message
    assert "forget" in message


class HangingConnection(FakeConnection):
    """A link whose disconnect never completes: no Disconnection Complete."""

    async def disconnect(self, reason=0) -> None:
        await asyncio.sleep(3600)


class HangingTransport:
    """A USB transport whose event thread never joins."""

    async def close(self) -> None:
        await asyncio.sleep(3600)


def test_teardown_is_bounded_when_the_iphone_disconnect_never_completes(
    backend, monkeypatch
):
    """A wedged disconnect must not keep the receiver thread (and USB) alive."""
    monkeypatch.setattr(ancs_backend, "TEARDOWN_STEP_TIMEOUT_S", 0.05)
    backend._connection = HangingConnection(HCI_STUB.Role.PERIPHERAL)

    started = time.monotonic()
    asyncio.run(backend._teardown_connection(disconnect=True))

    assert time.monotonic() - started < 2.0
    assert backend._connection is None


def test_closing_the_transport_is_bounded(backend, monkeypatch):
    monkeypatch.setattr(ancs_backend, "TEARDOWN_TRANSPORT_TIMEOUT_S", 0.05)
    backend._transport = HangingTransport()

    started = time.monotonic()
    asyncio.run(backend._close_transport())

    assert time.monotonic() - started < 2.0
    assert backend._transport is None


def test_the_failure_listener_is_removed(backend):
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL)

    wait_for_secure_link(backend, connection)

    assert connection._listeners.get(FakeConnection.EVENT_PAIRING_FAILURE) == []


class FakeDevice:
    """Stand-in for `bumble.device.Device`: the data blobs plus added services."""

    advertising_data = b""
    scan_response_data = b""

    def __init__(self, keystore=None):
        self.services = []
        self.keystore = keystore

    def add_service(self, service) -> None:
        self.services.append(service)


class FakeKeystore:
    """`bumble.keys.KeyStore` lookups, by peer address string."""

    def __init__(self, entries=None):
        self.entries = entries or {}

    async def get(self, name):
        return self.entries.get(name)


def _bond(backend, connection, *, ltk=True) -> None:
    backend._device = FakeDevice(
        keystore=FakeKeystore(
            {
                str(connection.peer_address): types.SimpleNamespace(
                    ltk=object() if ltk else None, ltk_peripheral=None
                )
            }
        )
    )


def test_a_bonded_reconnect_waits_for_the_iphone_instead_of_asking_again(backend):
    """A Security Request sent into the iPhone's own encryption breaks the bond."""
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL)
    _bond(backend, connection)

    async def run():
        asyncio.get_running_loop().call_later(
            0.01, setattr, connection, "is_encrypted", True
        )
        await backend._wait_for_secure_link(connection)

    asyncio.run(run())

    assert connection.requested == 0
    assert connection.is_encrypted is True


def test_a_bond_the_iphone_ignores_falls_back_to_a_security_request(backend, monkeypatch):
    monkeypatch.setattr(ancs_backend, "RECONNECT_ENCRYPTION_WAIT_S", 0.05)
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL)
    _bond(backend, connection)

    asyncio.run(backend._wait_for_secure_link(connection))

    assert connection.requested == 1
    assert connection.is_encrypted is True


def test_an_unbonded_iphone_is_asked_to_pair_right_away(backend):
    connection = FakeConnection(HCI_STUB.Role.PERIPHERAL)
    backend._device = FakeDevice(keystore=FakeKeystore())

    asyncio.run(backend._wait_for_secure_link(connection))

    assert connection.requested == 1


class FakePairing:
    """`bumble.pairing` without importing Bumble."""

    class PairingDelegate:
        NO_OUTPUT_NO_INPUT = "NO_OUTPUT_NO_INPUT"
        DISPLAY_OUTPUT_ONLY = "DISPLAY_OUTPUT_ONLY"

        def __init__(self, io_capability=None):
            self.io_capability = io_capability

    class AddressType:
        PUBLIC = 0
        RANDOM = 1


class FakePairingConfig:
    AddressType = FakePairing.AddressType

    def __init__(self, **options):
        self.options = options


FakePairing.PairingConfig = FakePairingConfig


def test_just_works_is_the_default_security(backend):
    delegate = backend._pairing_delegate(FakePairing)

    assert backend.mitm is False
    assert backend.bonding is True
    assert delegate.io_capability == FakePairing.PairingDelegate.NO_OUTPUT_NO_INPUT


def test_the_bonded_identity_is_the_advertised_static_address(backend):
    """iOS remembers the SMP identity address; it must be the one we advertise."""
    config = backend._pairing_config(FakePairing)

    assert config.options["identity_address_type"] == FakePairing.AddressType.RANDOM
    assert config.options["bonding"] is True
    assert config.options["sc"] is True


def test_the_local_irk_is_created_once_and_reused(tmp_path):
    keystore = tmp_path / "bluetooth" / "ancs-keys.json"
    backend = ancs_backend.BumbleAncsBackend(log=LOGGER, keystore_path=keystore)

    first = backend._load_or_create_irk()
    again = ancs_backend.BumbleAncsBackend(
        log=LOGGER, keystore_path=keystore
    )._load_or_create_irk()

    assert len(first) == ancs_backend.IRK_SIZE
    assert first != bytes(ancs_backend.IRK_SIZE)
    assert again == first
    assert (tmp_path / "bluetooth" / ancs_backend.IRK_FILE_NAME).read_bytes() == first

    # Without a key store path the IRK is ephemeral but still usable.
    ephemeral = ancs_backend.BumbleAncsBackend(log=LOGGER)._load_or_create_irk()
    assert len(ephemeral) == ancs_backend.IRK_SIZE


def test_mitm_switches_to_a_passkey_display():
    shown = []
    backend = ancs_backend.BumbleAncsBackend(log=LOGGER, mitm=True, on_passkey=None)
    backend.on_passkey = lambda number, digits: shown.append((number, digits))

    delegate = backend._pairing_delegate(FakePairing)
    assert delegate.io_capability == FakePairing.PairingDelegate.DISPLAY_OUTPUT_ONLY

    asyncio.run(delegate.display_number(123456, 6))
    assert shown == [(123456, 6)]


def test_the_security_summary_describes_the_link(backend):
    connection = FakeConnection(HCI_STUB.Role.CENTRAL, encrypted=True)
    connection.authenticated = True
    connection.sc = True
    connection.encryption_key_size = 16
    connection.pairing_peer_authentication_requirements = 0x04
    backend._connection = connection

    summary = backend.security_summary()

    assert summary["connected"] is True
    assert summary["encrypted"] is True
    assert summary["authenticated"] is True
    assert summary["secure_connections"] is True
    assert summary["key_size"] == 16
    assert summary["role"] == "CENTRAL"
    assert summary["peer_mitm_requested"] is True
    assert summary["bonding"] is True


def test_a_missing_connection_is_reported_rather_than_guessed(backend):
    summary = backend.security_summary()

    assert summary["connected"] is False
    assert summary["encrypted"] is False
    assert summary["mitm_requested"] is False


def test_the_controller_reports_what_it_broadcasts(backend):
    backend._device = FakeDevice()
    backend._device.advertising_data = bytes.fromhex(
        "0201061115d0002d121e4b0fa4994eceb531f405790319c000"
    )

    report = backend.advertising_report

    assert report is not None
    assert report.has_ancs_solicitation
    assert report.appearance_name == "Generic Watch"


def test_the_accessory_solicits_ancs_and_advertises_a_watch():
    """iOS keys on the 0x15 solicitation field, not on a plain UUID list."""
    core = pytest.importorskip("bumble.core")
    from bumble.gatt import GATT_ANCS_SERVICE  # noqa: PLC0415

    backend = ancs_backend.BumbleAncsBackend(log=LOGGER, hid=False)
    backend._device = FakeDevice()

    backend._configure_advertising(core)
    advertising = core.AdvertisingData.from_bytes(backend._device.advertising_data)
    structures = dict(advertising.ad_structures)
    assert structures[core.AdvertisingData.LIST_OF_128_BIT_SERVICE_SOLICITATION_UUIDS] == (
        GATT_ANCS_SERVICE.uuid_bytes
    )
    assert structures[core.AdvertisingData.APPEARANCE] == ancs_backend.ANCS_APPEARANCE.to_bytes(
        2, "little"
    )
    # 0x15 must be the solicitation AD type, not a UUID list entry.
    assert not any(
        ad_type
        in (
            core.AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
            core.AdvertisingData.INCOMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
        )
        for ad_type in structures
    )
    # A legacy advertising PDU holds 31 bytes.
    assert len(backend._device.advertising_data) <= 31

    response = core.AdvertisingData.from_bytes(backend._device.scan_response_data)
    assert dict(response.ad_structures)[core.AdvertisingData.COMPLETE_LOCAL_NAME] == (
        ancs_backend.ANCS_ADVERTISED_NAME.encode("utf-8")
    )


def test_hid_is_on_by_default_and_advertised(backend):
    """HID 0x1812 in the 0x03 list is what makes iOS list the accessory."""
    core = pytest.importorskip("bumble.core")
    from bumble.gatt import GATT_HUMAN_INTERFACE_DEVICE_SERVICE  # noqa: PLC0415

    assert backend.hid is True
    backend._device = FakeDevice()

    backend._configure_advertising(core)

    advertising = core.AdvertisingData.from_bytes(backend._device.advertising_data)
    structures = dict(advertising.ad_structures)
    assert structures[core.AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS] == (
        bytes([0x12, 0x18])
    )
    assert structures[core.AdvertisingData.APPEARANCE] == bytes([0xC1, 0x03])
    assert len(backend._device.advertising_data) <= 31

    report = backend.advertising_report
    assert report.has_hid_service
    assert report.has_ancs_solicitation
    assert report.appearance_name == "Keyboard"
    assert GATT_HUMAN_INTERFACE_DEVICE_SERVICE.uuid_bytes == bytes([0x12, 0x18])


def test_the_hid_keyboard_service_is_published(backend):
    pytest.importorskip("bumble")
    backend._device = FakeDevice()

    backend._add_hid_service()

    assert backend.hid_keyboard is not None
    assert [str(service.uuid) for service in backend._device.services] == [
        "UUID-16:1812 (Human Interface Device)"
    ]


def test_hid_can_be_switched_off(monkeypatch):
    pytest.importorskip("bumble")
    backend = ancs_backend.BumbleAncsBackend(log=LOGGER, hid=False)

    assert backend.hid is False
    assert backend.appearance == ancs_backend.ANCS_APPEARANCE
    assert backend.security_summary()["hid"] is False
