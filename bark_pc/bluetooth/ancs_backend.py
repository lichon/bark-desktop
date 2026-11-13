"""Bumble implementation of the ANCS receiver.

The app always uses one connection direction:

* **peripheral** — advertise an ANCS service solicitation named “Bark Desktop”
  so the iPhone can connect from its Settings → Bluetooth screen.

The diagnostic CLI (`bark_pc.tools.ancs_connect`) also drives the opposite role:
it can scan for nearby Apple devices and connect to one of them.

Common flow:

1. open the selected USB Bluetooth HCI adapter through libusb,
2. obtain a connection (advertise/wait, or scan/connect from the CLI),
3. pair/bond (the iPhone asks for it; the keys are cached in the data folder),
4. discover the ANCS service and subscribe to Notification Source + Data Source,
5. request the title/subtitle/message/app attributes for every new notification.

Nothing here touches Windows' own Bluetooth stack: the dongle belongs to Bumble
while the receiver runs, and the iPhone sees one extra BLE accessory.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from .advertising import (
    ANCS_SERVICE_UUID_BYTES,
    HID_APPEARANCE_KEYBOARD,
    HID_SERVICE_UUID_16,
    AdvertisingReport,
    AncsAdvertising,
    analyze_advertising,
)
from .ancs import (
    ANCS_ADVERTISED_NAME,
    ANCS_APPEARANCE,
    AncsError,
    AncsNotification,
    AncsPeerInfo,
    advertisement_peer,
    notification_from_attributes,
)

logger = logging.getLogger("bark_pc.ancs")

PAIR_TIMEOUT_S = 90.0
SCAN_TIMEOUT_S = 8.0
CONNECT_TIMEOUT_S = 25.0
ANCS_DISCOVERY_TIMEOUT_S = 30.0
ATTRIBUTE_TIMEOUT_S = 20.0
APP_NAME_TIMEOUT_S = 10.0

# Title/subtitle/message are length-limited when requested.  These are Apple's
# recommended maxima (ANCS spec); larger values are rejected by some iOS builds.
ATTRIBUTE_MAX_LENGTHS = {
    "TITLE": 255,
    "SUBTITLE": 255,
    "MESSAGE": 512,
}

# Driver-facing hint shown when libusb cannot claim the interface.
DRIVER_HINT = (
    "Install the WinUSB driver for the adapter with Zadig, then replug it. "
    "The Windows Bluetooth driver cannot be shared with Bumble."
)


def _require_bumble():
    """Import Bumble lazily so this module is importable without it."""
    try:
        from bumble import core, device, hci, keys, pairing  # noqa: PLC0415
        from bumble.profiles import ancs as ancs_profile  # noqa: PLC0415
        from bumble.transport import open_transport  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - depends on the installation
        raise AncsError(
            "Bumble is not installed, so iPhone notifications over Bluetooth are "
            f"unavailable ({exc})."
        ) from exc
    return core, device, hci, keys, pairing, ancs_profile, open_transport


class BumbleAncsBackend:
    """One USB adapter, one advertising Bumble device, one iPhone at a time."""

    def __init__(
        self,
        log: logging.Logger | None = None,
        keystore_path=None,
        adapter: str = "",
        *,
        appearance: int | None = None,
        mitm: bool = False,
        bonding: bool = True,
        on_passkey: Callable[[int, int], None] | None = None,
        hid: bool = True,
    ):
        self.log = log or logger
        self.keystore_path = Path(keystore_path) if keystore_path else None
        self.adapter = (adapter or "").strip()

        # Appearance is cosmetic: it picks the iOS icon, never the ANCS flow.
        # A HID keyboard is advertised as a keyboard unless told otherwise,
        # because that is the icon and label iOS pairs with a peripheral HID.
        self.appearance = (
            (HID_APPEARANCE_KEYBOARD if hid else ANCS_APPEARANCE)
            if appearance is None
            else int(appearance)
        )
        # HID over GATT is what makes iOS list the accessory in Settings;
        # ANCS alone is often invisible until a bond already exists.
        self.hid = bool(hid)
        self.hid_keyboard = None
        # MITM needs a channel that can show or type a passkey; the accessory
        # has none, so the app keeps Just Works + bonding (iOS still requires
        # an encrypted link, and the bond survives address rotation).
        self.mitm = bool(mitm)
        self.bonding = bool(bonding)
        # Called with (number, digits) when the pairing asks us to display one.
        self.on_passkey = on_passkey

        self._transport = None
        self._device = None
        self._connection = None
        self._client = None
        self._process_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[object] | None = None
        self._on_notification: Callable[[AncsNotification], None] | None = None
        self._on_disconnected: Callable[[], None] | None = None
        self._disconnect_listener = None
        self._app_names: dict[str, str] = {}

    # -- lifecycle --------------------------------------------------------- #
    async def start(self) -> None:
        core, device_module, hci, keys, pairing, _ancs, open_transport = _require_bumble()
        adapter = self.adapter
        if not adapter:
            raise AncsError("Choose a USB Bluetooth adapter first.")
        if not adapter.lower().startswith("usb:"):
            raise AncsError("The selected adapter is not a USB Bluetooth adapter.")

        try:
            self._transport = await open_transport(adapter)
        except Exception as exc:
            raise AncsError(_open_error_message(exc)) from exc

        config = device_module.DeviceConfiguration(
            name=ANCS_ADVERTISED_NAME,
            # A fixed static address keeps the iPhone's bond valid across runs.
            address=hci.Address("F0:F1:F2:F3:F4:F5"),
        )
        try:
            self._device = device_module.Device.from_config_with_hci(
                config, self._transport.source, self._transport.sink
            )
            if self.keystore_path is not None:
                try:
                    self.keystore_path.parent.mkdir(parents=True, exist_ok=True)
                    # Install the key store before power_on so the controller's
                    # resolving list is built from the iPhone's IRK on reconnect.
                    self._device.keystore = keys.JsonKeyStore.from_device(
                        self._device, filename=str(self.keystore_path)
                    )
                    self.log.info("ancs: pairing keys in %s", self.keystore_path)
                except Exception:  # pragma: no cover - read-only data folder
                    self.log.warning(
                        "ancs: could not open the pairing key store; the iPhone "
                        "may ask to pair again",
                        exc_info=True,
                    )
            # ANCS only needs an encrypted, bonded link.  Just Works is the one
            # pairing method an accessory without a display can complete;
            # `--mitm` switches to passkey entry and shows the number instead.
            self._device.pairing_config_factory = lambda _connection: pairing.PairingConfig(
                sc=True,
                mitm=self.mitm,
                bonding=self.bonding,
                delegate=self._pairing_delegate(pairing),
            )
            await self._device.power_on()
            if self.hid:
                self._add_hid_service()
        except Exception as exc:
            await self._close_transport()
            raise AncsError(f"Could not start the Bluetooth controller: {exc}") from exc

        self._configure_advertising(core)
        self.log.info("ancs: opened %s", adapter)

    def _add_hid_service(self) -> None:
        """Publish the HOGP keyboard so iOS has a profile it always lists."""
        from .hid import HidKeyboard  # noqa: PLC0415 - lazy, needs Bumble

        assert self._device is not None
        keyboard = HidKeyboard(
            on_output_report=lambda leds: self.log.info("ancs: keyboard LEDs 0x%02X", leds)
        )
        self._device.add_service(keyboard)
        self.hid_keyboard = keyboard
        self.log.info("ancs: HID keyboard service 0x%04X published", HID_SERVICE_UUID_16)

    def _pairing_delegate(self, pairing):
        """Just Works by default; passkey entry when MITM is requested."""
        io_capability = (
            pairing.PairingDelegate.DISPLAY_OUTPUT_ONLY
            if self.mitm
            else pairing.PairingDelegate.NO_OUTPUT_NO_INPUT
        )
        on_passkey = self.on_passkey
        log = self.log

        class AncsPairingDelegate(pairing.PairingDelegate):
            def __init__(self) -> None:
                super().__init__(io_capability=io_capability)

            async def display_number(self, number: int, digits: int) -> None:
                log.info("ancs: pairing passkey %0*d", digits, number)
                if on_passkey is not None:
                    try:
                        on_passkey(number, digits)
                    except Exception:  # pragma: no cover - callback isolation
                        log.debug("ancs: passkey callback failed", exc_info=True)

            async def compare_numbers(self, number: int, digits: int) -> bool:
                await self.display_number(number, digits)
                return True

        return AncsPairingDelegate()

    def _configure_advertising(self, core) -> None:
        advertising = ancs_advertising_data(self.appearance, hid=self.hid, core=core)
        assert self._device is not None
        self._device.advertising_data = advertising.advertising
        self._device.scan_response_data = advertising.scan_response

        report = advertising.report
        self.log.info("ancs: %s", report.describe().replace("\n", " | "))
        for warning in report.warnings():
            self.log.warning("ancs: advertising: %s", warning)

    @property
    def advertising_report(self) -> AdvertisingReport | None:
        """What this controller is broadcasting right now, decoded."""
        if self._device is None:
            return None
        return analyze_advertising(bytes(getattr(self._device, "advertising_data", b"")))

    async def advertise(self) -> None:
        if self._device is None:
            raise AncsError("The Bluetooth controller is not open.")
        try:
            await self._device.start_advertising(auto_restart=False)
        except Exception as exc:
            raise AncsError(f"Could not advertise the ANCS device: {exc}") from exc

    # -- connection -------------------------------------------------------- #
    async def scan(
        self, stop_event: asyncio.Event, *, include_all: bool = False
    ) -> list[AncsPeerInfo]:
        """Collect iPhone candidates for a few seconds.

        `include_all` returns every advertising device instead of only Apple
        devices the ANCS receiver could use (the diagnostic CLI uses it).
        """
        core, _device, _hci, _keys, _pairing, _ancs, _transport = _require_bumble()
        from bumble.gatt import GATT_ANCS_SERVICE  # noqa: PLC0415

        if self._device is None:
            raise AncsError("The Bluetooth controller is not open.")
        found: dict[str, AncsPeerInfo] = {}

        def on_advertisement(advertisement) -> None:
            try:
                peer = _advertisement_to_peer(
                    advertisement, core, GATT_ANCS_SERVICE, include_all=include_all
                )
            except Exception:  # pragma: no cover - malformed advertisement
                return
            if peer is None:
                return
            previous = found.get(peer.address)
            if previous is None:
                found[peer.address] = peer
                return
            found[peer.address] = AncsPeerInfo(
                address=peer.address,
                name=peer.name or previous.name,
                rssi=max(peer.rssi, previous.rssi),
                apple=peer.apple or previous.apple,
                has_ancs=peer.has_ancs or previous.has_ancs,
                connectable=peer.connectable or previous.connectable,
            )

        device = self._device
        device.on(device.EVENT_ADVERTISEMENT, on_advertisement)
        try:
            try:
                await device.start_scanning(active=True, filter_duplicates=False)
            except Exception as exc:
                raise AncsError(f"Could not start scanning for the iPhone: {exc}") from exc
            try:
                await asyncio.wait_for(stop_event.wait(), SCAN_TIMEOUT_S)
            except asyncio.TimeoutError:
                pass
        finally:
            try:
                await device.stop_scanning()
            except Exception:  # pragma: no cover - teardown
                self.log.debug("ancs: stopping the scan failed", exc_info=True)
            try:
                device.remove_listener(device.EVENT_ADVERTISEMENT, on_advertisement)
            except Exception:  # pragma: no cover - already removed
                pass
        return list(found.values())

    async def connect(self, peer: AncsPeerInfo) -> None:
        """Open an LE connection to a scanned iPhone (central role)."""
        _core, _device, hci, _keys, _pairing, _ancs, _transport = _require_bumble()
        if self._device is None:
            raise AncsError("The Bluetooth controller is not open.")
        if self._connection is not None:
            await self._teardown_connection(disconnect=True)
        try:
            connection = await asyncio.wait_for(
                self._device.connect_le(peer.address, own_address_type=hci.OwnAddressType.RANDOM),
                CONNECT_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            raise AncsError(
                f"Timed out connecting to {peer.label}. Keep the iPhone's "
                "Settings → Bluetooth screen open and try again."
            ) from exc
        except Exception as exc:
            raise AncsError(
                f"Could not connect to {peer.label}. Open Settings → Bluetooth on "
                f"the iPhone and keep the screen on ({exc})."
            ) from exc
        self._connection = connection
        self.log.info("ancs: connected to %s", peer.label)

    async def wait_for_connection(self, stop_event: asyncio.Event) -> AncsPeerInfo | None:
        assert self._device is not None
        loop = asyncio.get_running_loop()
        connected: asyncio.Future = loop.create_future()

        def on_connection(connection) -> None:
            if not connected.done():
                connected.set_result(connection)

        def on_connection_failure(error) -> None:
            if not connected.done():
                connected.set_exception(AncsError(f"Bluetooth connection failed: {error}"))

        device = self._device
        device.on(device.EVENT_CONNECTION, on_connection)
        device.on(device.EVENT_CONNECTION_FAILURE, on_connection_failure)
        stop_task = asyncio.ensure_future(stop_event.wait())
        try:
            await self.advertise()
            await asyncio.wait({connected, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not connected.done():
                # Stop was requested while waiting for the iPhone.
                try:
                    await self._device.stop_advertising()
                except Exception:  # pragma: no cover - teardown
                    pass
                return None
            connection = connected.result()
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            try:
                device.remove_listener(device.EVENT_CONNECTION, on_connection)
                device.remove_listener(device.EVENT_CONNECTION_FAILURE, on_connection_failure)
            except Exception:  # pragma: no cover - already removed
                pass

        self._connection = connection
        name = await self._request_peer_name(connection)
        return AncsPeerInfo(address=str(connection.peer_address), name=name)

    async def _request_peer_name(self, connection) -> str:
        _core, device_module, _hci, _keys, _pairing, _ancs, _transport = _require_bumble()
        peer = device_module.Peer(connection)
        try:
            return (await asyncio.wait_for(peer.request_name(), 5.0)).strip()
        except Exception:  # pragma: no cover - iPhone may keep GAP private
            self.log.debug("ancs: could not read the iPhone's name", exc_info=True)
            return ""

    async def subscribe(
        self,
        on_notification: Callable[[AncsNotification], None],
        on_disconnected: Callable[[], None],
    ) -> object:
        _core, device_module, _hci, _keys, _pairing, ancs_profile, _transport = _require_bumble()
        connection = self._connection
        if connection is None:
            raise AncsError("The iPhone is not connected.")
        self._on_notification = on_notification
        self._on_disconnected = on_disconnected

        try:
            await self._wait_for_secure_link(connection)
        except asyncio.TimeoutError:
            self.log.warning("ancs: pairing did not finish; trying the ANCS subscription anyway")

        peer = device_module.Peer(connection)
        try:
            client = await asyncio.wait_for(
                ancs_profile.AncsClient.for_peer(peer), ANCS_DISCOVERY_TIMEOUT_S
            )
        except Exception as exc:
            raise AncsError(
                "The iPhone did not expose the Apple Notification Center Service. "
                "Make sure iOS is up to date and pair the accessory again."
            ) from exc
        if client is None:
            raise AncsError(
                "The iPhone did not expose the Apple Notification Center Service. "
                "Unpair the accessory in Settings → Bluetooth and pair it again."
            )

        try:
            await client.start()
        except Exception as exc:
            raise AncsError(f"Could not subscribe to iPhone notifications: {exc}") from exc
        self._client = client
        client.on(client.EVENT_NOTIFICATION, self._enqueue_notification)

        def on_disconnect(_reason=None) -> None:
            try:
                connection.remove_listener(connection.EVENT_DISCONNECTION, on_disconnect)
            except Exception:  # pragma: no cover - already removed
                pass
            if self._connection is connection:
                self._connection = None
            if self._disconnect_listener is on_disconnect:
                self._disconnect_listener = None
            self._client = None
            task, self._process_task = self._process_task, None
            if task is not None:
                task.cancel()
            self._queue = None
            self._notify_disconnected()

        self._disconnect_listener = on_disconnect
        try:
            connection.on(connection.EVENT_DISCONNECTION, on_disconnect)
        except Exception:  # pragma: no cover - event API differences
            self.log.debug("ancs: could not watch the connection", exc_info=True)

        self._queue = asyncio.Queue(maxsize=64)
        self._process_task = asyncio.create_task(
            self._process_notifications(client), name="bark-ancs-notifications"
        )
        self.log.info("ancs: subscribed to Apple notifications")
        return client

    async def disconnect(self, handle: object | None = None) -> None:
        await self._teardown_connection(disconnect=True)

    def security_summary(self) -> dict[str, object]:
        """Bonding/MITM facts about the current link, for the CLI to print."""
        connection = self._connection
        summary: dict[str, object] = {
            "connected": connection is not None,
            "bonding": self.bonding,
            "mitm_requested": self.mitm,
            "hid": self.hid,
            "encrypted": False,
            "authenticated": False,
            "secure_connections": False,
            "key_size": 0,
            "role": "",
            "peer_mitm_requested": False,
        }
        if connection is None:
            return summary
        requirements = int(getattr(connection, "pairing_peer_authentication_requirements", 0) or 0)
        summary.update(
            encrypted=bool(getattr(connection, "is_encrypted", False)),
            authenticated=bool(getattr(connection, "authenticated", False)),
            secure_connections=bool(getattr(connection, "sc", False)),
            key_size=int(getattr(connection, "encryption_key_size", 0) or 0),
            role=str(getattr(connection, "role_name", "") or ""),
            peer_mitm_requested=bool(requirements & 0x04),
        )
        return summary

    async def _start_pairing(self, connection) -> asyncio.Future | None:
        """Ask for an encrypted link the way our GAP role allows.

        Only the central may send an SMP Pairing Request.  In peripheral mode the
        iPhone is the central, so we send a Security Request and let iOS start
        the pairing; a Pairing Request from the peripheral side is answered with
        ``UNSPECIFIED_REASON`` and the bond never happens.  Returns the pending
        ``pair()`` task in central mode, ``None`` when the iPhone has to lead.
        """
        _core, _device, hci, _keys, _pairing, _ancs, _open = _require_bumble()
        if connection.role == hci.Role.PERIPHERAL:
            self.log.info("ancs: requesting pairing; the iPhone starts it as central")
            connection.request_pairing()
            return None
        return asyncio.ensure_future(connection.pair())

    async def _wait_for_secure_link(self, connection) -> None:
        """Wait for bonding, whether the iPhone or this accessory starts it."""
        if connection.is_encrypted:
            return
        loop = asyncio.get_running_loop()
        pair_task = await self._start_pairing(connection)

        failure: asyncio.Future | None = None
        on_failure = None
        if pair_task is None:
            # Peripheral mode: the iPhone pairs as central, so watch its answer.
            failure = loop.create_future()

            def on_failure(reason=None) -> None:
                if not failure.done():
                    failure.set_result(reason)

            connection.on(connection.EVENT_PAIRING_FAILURE, on_failure)

        deadline = loop.time() + PAIR_TIMEOUT_S
        try:
            while not connection.is_encrypted and loop.time() < deadline:
                if failure is not None and failure.done():
                    reason = failure.result()
                    name = getattr(reason, "name", reason)
                    raise AncsError(
                        f"The iPhone refused to pair ({name}). On the iPhone open "
                        f"Settings → Bluetooth, forget “{ANCS_ADVERTISED_NAME}” and "
                        "pair again; if it keeps failing, delete the pairing-key "
                        "file so both sides start from scratch."
                    )
                if pair_task is not None and pair_task.done():
                    error = None if pair_task.cancelled() else pair_task.exception()
                    if error is not None:
                        # An iPhone-initiated pairing may already be in progress;
                        # that is not fatal while the link is still working.
                        self.log.debug("ancs: pairing call failed: %s", error)
                await asyncio.sleep(0.2)
            if not connection.is_encrypted:
                raise asyncio.TimeoutError()
        finally:
            if on_failure is not None:
                try:
                    connection.remove_listener(connection.EVENT_PAIRING_FAILURE, on_failure)
                except Exception:  # pragma: no cover - already removed
                    pass
            if pair_task is not None and not pair_task.done():
                pair_task.cancel()
            if pair_task is not None:
                await asyncio.gather(pair_task, return_exceptions=True)

    async def close(self) -> None:
        await self._teardown_connection(disconnect=True)
        if self._device is not None:
            try:
                await self._device.stop_advertising()
            except Exception:  # pragma: no cover - already stopped
                pass
        self._device = None
        await self._close_transport()

    async def _close_transport(self) -> None:
        transport, self._transport = self._transport, None
        if transport is None:
            return
        try:
            await transport.close()
        except Exception:  # pragma: no cover - libusb teardown
            self.log.debug("ancs: closing the USB transport failed", exc_info=True)

    async def _teardown_connection(self, *, disconnect: bool) -> None:
        task, self._process_task = self._process_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._queue = None

        connection, self._connection = self._connection, None
        listener, self._disconnect_listener = self._disconnect_listener, None
        client, self._client = self._client, None
        if connection is not None and listener is not None:
            try:
                connection.remove_listener(connection.EVENT_DISCONNECTION, listener)
            except Exception:  # pragma: no cover - already removed
                pass
        if client is not None:
            try:
                await client.stop()
            except Exception:  # pragma: no cover - link already gone
                self.log.debug("ancs: stopping the ANCS client failed", exc_info=True)
        if connection is not None and disconnect:
            try:
                await connection.disconnect()
            except Exception:  # pragma: no cover - already disconnected
                self.log.debug("ancs: disconnecting the iPhone failed", exc_info=True)

    # -- notifications ----------------------------------------------------- #
    def _enqueue_notification(self, notification) -> None:
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(notification)
        except asyncio.QueueFull:  # pragma: no cover - iPhone floods us
            self.log.warning("ancs: notification queue full; dropping the oldest")
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(notification)
            except Exception:  # pragma: no cover - race with teardown
                pass

    async def _process_notifications(self, client) -> None:
        _core, _device, _hci, _keys, _pairing, ancs_profile, _transport = _require_bumble()
        while True:
            notification = await self._queue.get()
            try:
                await self._handle_notification(client, ancs_profile, notification)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - iPhone/ATT specifics
                self.log.warning(
                    "ancs: could not read notification %s: %s",
                    getattr(notification, "notification_uid", "?"),
                    exc,
                )

    async def _handle_notification(self, client, ancs_profile, notification) -> None:
        if notification.event_flags & ancs_profile.EventFlags.PRE_EXISTING:
            # Replayed history after a reconnect: it is already in the history.
            return
        if notification.event_id == ancs_profile.EventId.NOTIFICATION_REMOVED:
            return
        if notification.event_id not in (
            ancs_profile.EventId.NOTIFICATION_ADDED,
            ancs_profile.EventId.NOTIFICATION_MODIFIED,
        ):
            return

        requested = [
            ancs_profile.NotificationAttributeId.APP_IDENTIFIER,
            (
                ancs_profile.NotificationAttributeId.TITLE,
                ATTRIBUTE_MAX_LENGTHS["TITLE"],
            ),
            (
                ancs_profile.NotificationAttributeId.SUBTITLE,
                ATTRIBUTE_MAX_LENGTHS["SUBTITLE"],
            ),
            (
                ancs_profile.NotificationAttributeId.MESSAGE,
                ATTRIBUTE_MAX_LENGTHS["MESSAGE"],
            ),
            ancs_profile.NotificationAttributeId.DATE,
        ]
        attributes = await asyncio.wait_for(
            client.get_notification_attributes(notification.notification_uid, requested),
            ATTRIBUTE_TIMEOUT_S,
        )
        pairs = [(attribute.attribute_id, attribute.value) for attribute in attributes]
        app_id = next(
            (
                str(value)
                for attribute_id, value in pairs
                if getattr(attribute_id, "name", "") == "APP_IDENTIFIER"
            ),
            "",
        )
        app_name = await self._app_name(client, ancs_profile, app_id)
        mapped = notification_from_attributes(
            uid=int(notification.notification_uid),
            category=getattr(getattr(notification, "category_id", ""), "name", ""),
            attributes=pairs,
            app_name=app_name,
        )
        callback = self._on_notification
        if callback is not None:
            callback(mapped)

    async def _app_name(self, client, ancs_profile, app_id: str) -> str:
        if not app_id:
            return ""
        cached = self._app_names.get(app_id)
        if cached is not None:
            return cached
        try:
            attributes = await asyncio.wait_for(
                client.get_app_attributes(app_id, [ancs_profile.AppAttributeId.DISPLAY_NAME]),
                APP_NAME_TIMEOUT_S,
            )
            name = next(
                (
                    str(attribute.value).strip()
                    for attribute in attributes
                    if getattr(attribute.attribute_id, "name", "") == "DISPLAY_NAME"
                ),
                "",
            )
        except Exception:  # pragma: no cover - some apps have no display name
            self.log.debug("ancs: could not read the app name for %s", app_id, exc_info=True)
            name = ""
        # Cache negative answers too: a missing display name never appears later.
        self._app_names[app_id] = name or app_id
        return self._app_names[app_id]

    # -- callbacks --------------------------------------------------------- #
    def _notify_disconnected(self) -> None:
        callback = self._on_disconnected
        self._on_disconnected = None
        if callback is not None:
            try:
                callback()
            except Exception:  # pragma: no cover - callback isolation
                self.log.exception("ancs: disconnect callback failed")


def ancs_advertising_data(
    appearance: int | None = None, *, hid: bool = False, core=None
) -> AncsAdvertising:
    """Build the accessory's advertising PDU and scan response as raw bytes.

    Kept separate from `_configure_advertising` so the CLI can print the exact
    packet (and the tests can parse it) without opening a controller.

    With *hid* the packet also lists service 0x1812, which is the profile that
    makes iOS show the accessory in Settings → Bluetooth; without it the
    solicitation alone is easy for iOS to ignore.
    """
    if core is None:
        core, _device, _hci, _keys, _pairing, _ancs, _transport = _require_bumble()
    from bumble.gatt import GATT_ANCS_SERVICE  # noqa: PLC0415

    if appearance is None:
        value = HID_APPEARANCE_KEYBOARD if hid else ANCS_APPEARANCE
    else:
        value = int(appearance)
    if not 0 <= value <= 0xFFFF:
        raise AncsError(f"Appearance 0x{value:X} does not fit in the 16-bit field.")

    structures = [
        (
            core.AdvertisingData.FLAGS,
            bytes(
                [
                    core.AdvertisingData.Flags.LE_GENERAL_DISCOVERABLE_MODE
                    | core.AdvertisingData.Flags.BR_EDR_NOT_SUPPORTED
                ]
            ),
        ),
        # AD type 0x15 with the ANCS UUID bytes: this solicitation is what
        # makes the iPhone list the accessory with an ANCS pairing prompt.
        (
            core.AdvertisingData.LIST_OF_128_BIT_SERVICE_SOLICITATION_UUIDS,
            GATT_ANCS_SERVICE.uuid_bytes,
        ),
    ]
    if hid:
        # AD type 0x03 (16-bit service list) with HID over GATT (0x1812).
        structures.append(
            (
                core.AdvertisingData.COMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS,
                HID_SERVICE_UUID_16.to_bytes(2, "little"),
            )
        )
    # The appearance only changes the iOS icon, never the ANCS flow.
    structures.append(
        (
            core.AdvertisingData.APPEARANCE,
            value.to_bytes(2, "little"),
        )
    )
    advertising = core.AdvertisingData(structures)
    scan_response = core.AdvertisingData(
        [
            (
                core.AdvertisingData.COMPLETE_LOCAL_NAME,
                ANCS_ADVERTISED_NAME.encode("utf-8"),
            )
        ]
    )
    return AncsAdvertising(
        advertising=bytes(advertising), scan_response=bytes(scan_response), hid=hid
    )


def _advertisement_to_peer(
    advertisement, core, ancs_service, *, include_all: bool = False
) -> AncsPeerInfo | None:
    """Decode the fields we care about from one Bumble advertisement."""
    data = advertisement.data
    name = ""
    for type_id in (
        core.AdvertisingData.COMPLETE_LOCAL_NAME,
        core.AdvertisingData.SHORTENED_LOCAL_NAME,
    ):
        try:
            value = data.get(type_id)
        except Exception:  # pragma: no cover - malformed name
            value = None
        if value:
            name = str(value)
            break

    apple = False
    try:
        manufacturer_data = data.get_all(core.AdvertisingData.MANUFACTURER_SPECIFIC_DATA, raw=True)
    except Exception:  # pragma: no cover - malformed manufacturer data
        manufacturer_data = []
    for raw in manufacturer_data or []:
        if len(raw) >= 2 and int.from_bytes(raw[:2], "little") == 0x004C:
            apple = True
            break

    has_ancs = False
    # 0x15 (service solicitation) is how an ANCS accessory announces itself; a
    # 0x06/0x07 list is the other order of the same relationship and both mean
    # "ANCS is talked about here".  Checking the solicitation too keeps the scan
    # list consistent with what this backend advertises.
    try:
        solicitations = data.get_all(
            core.AdvertisingData.LIST_OF_128_BIT_SERVICE_SOLICITATION_UUIDS, raw=True
        )
    except Exception:  # pragma: no cover - malformed solicitation list
        solicitations = []
    for raw in solicitations or []:
        if ANCS_SERVICE_UUID_BYTES in bytes(raw):
            has_ancs = True
            break

    for type_id in (
        core.AdvertisingData.COMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
        core.AdvertisingData.INCOMPLETE_LIST_OF_128_BIT_SERVICE_CLASS_UUIDS,
    ):
        try:
            uuids = data.get_all(type_id)
        except Exception:  # pragma: no cover - malformed UUID list
            uuids = []
        if any(uuid == ancs_service for uuid in uuids or []):
            has_ancs = True
            break

    return advertisement_peer(
        address=str(advertisement.address),
        name=name,
        rssi=advertisement.rssi,
        connectable=advertisement.is_connectable,
        apple=apple,
        has_ancs=has_ancs,
        include_all=include_all,
    )


def _open_error_message(exc: Exception) -> str:
    text = str(exc)
    lowered = text.lower()
    if "device not found" in lowered:
        return "The selected USB Bluetooth adapter is not plugged in. Replug it and press Restart."
    if "no compatible interface" in lowered:
        return f"The USB adapter cannot be used as a Bluetooth controller. {DRIVER_HINT}"
    if "access" in lowered or "busy" in lowered or "not supported" in lowered:
        return f"Windows did not let Bumble open the USB adapter. {DRIVER_HINT}"
    return f"Could not open the USB Bluetooth adapter: {text}. {DRIVER_HINT}"
