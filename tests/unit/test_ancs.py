"""ANCS receiver: mapping, settings, and the worker state machine.

No test opens a USB dongle: `FakeAncsBackend` replaces Bumble and only records
calls / delivers scripted connections and notifications.  The radio flow itself
is covered by the manual checklist in docs/validation/Bluetooth.md.
"""

from __future__ import annotations

import asyncio
import threading
import time
from enum import IntEnum

from bark_pc.bluetooth import (
    AncsError,
    AncsNotification,
    AncsPeerInfo,
    AncsState,
    AncsStatus,
    AncsWorker,
    describe_ancs_state,
)
from bark_pc.bluetooth.ancs import (
    advertisement_peer,
    notification_from_attributes,
)
from bark_pc.core.config import Settings, SettingsStore
from bark_pc.push.ingest import Ingestor
from bark_pc.push.payload import ParseOutcome


class FakeAncsBackend:
    """Scripted backend: connections and notifications arrive from the test."""

    def __init__(self, log, keystore_path, adapter):
        self.log = log
        self.keystore_path = keystore_path
        self.adapter = adapter
        self.started = False
        self.closed = False
        self.disconnected = 0
        self.connect_attempts: list[tuple[str, str, float]] = []
        self.fail_connect: str | None = None
        self._pending: list[AncsPeerInfo] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_notification = None
        self._on_disconnected = None

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.started = True

    async def wait_for_connection(self, stop_event):
        while not stop_event.is_set():
            with self._lock:
                if self._pending:
                    return self._pending.pop(0)
            await asyncio.sleep(0.01)
        return None

    async def connect(self, peer, *, timeout=45.0):
        self._loop = asyncio.get_running_loop()
        self.connect_attempts.append((peer.address, peer.name, timeout))
        if self.fail_connect:
            raise AncsError(self.fail_connect)

    async def subscribe(self, on_notification, on_disconnected):
        self._loop = asyncio.get_running_loop()
        self._on_notification = on_notification
        self._on_disconnected = on_disconnected
        return object()

    async def disconnect(self, handle=None):
        self.disconnected += 1

    async def close(self):
        self.closed = True

    # -- test controls ---------------------------------------------------- #
    def deliver_peer(self, name="iPhone", address="AA:BB:CC:DD:EE:FF") -> None:
        with self._lock:
            self._pending.append(AncsPeerInfo(address=address, name=name))

    def emit(self, notification: AncsNotification) -> None:
        assert self._on_notification is not None
        self._on_notification(notification)

    def drop_connection(self) -> None:
        assert self._on_disconnected is not None
        self._loop.call_soon_threadsafe(self._on_disconnected)


class _BackendRegistry:
    def __init__(self, fail_start: str | None = None, backend_class=FakeAncsBackend):
        self.fail_start = fail_start
        self.backend_class = backend_class
        self.instances: list[FakeAncsBackend] = []
        self.lock = threading.Lock()

    def __call__(self, log, keystore_path, adapter):
        backend = self.backend_class(log, keystore_path, adapter)
        if self.fail_start:
            async def failing_start():
                raise AncsError(self.fail_start)

            backend.start = failing_start  # type: ignore[method-assign]
        with self.lock:
            self.instances.append(backend)
        return backend

    @property
    def latest(self) -> FakeAncsBackend:
        with self.lock:
            return self.instances[-1]


def wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_worker(settings: Settings, registry: _BackendRegistry, received=None, **kwargs):
    return AncsWorker(
        settings,
        backend_factory=registry,
        on_notification=(received.append if received is not None else None),
        **kwargs,
    )


def sample_notification() -> AncsNotification:
    return AncsNotification(
        uid=42,
        app_id="com.apple.MobileSMS",
        app_name="Messages",
        title="Ada",
        subtitle="",
        body="Dinner at 8?",
        category="SOCIAL",
        date_ms=1_700_000_000_000,
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_ancs_is_off_by_default(settings_store):
    settings = settings_store.load()
    assert settings.ancs_enabled is False
    assert settings.ancs_adapter == ""
    assert settings.ancs_adapter_name == ""


def test_ancs_values_survive_a_settings_round_trip(portable_paths):
    store = SettingsStore(portable_paths)
    store.update(
        ancs_enabled=True,
        ancs_adapter="usb:0A12:0001",
        ancs_adapter_name="  CSR8510 A10  ",
    )
    loaded = SettingsStore(portable_paths).load()
    assert loaded.ancs_enabled is True
    assert loaded.ancs_adapter == "usb:0A12:0001"
    assert loaded.ancs_adapter_name == "CSR8510 A10"


def test_ancs_rejects_non_usb_transports(portable_paths):
    portable_paths.settings_path.write_text(
        '{"ancs_adapter": "tcp-client:example.com:1234"}', encoding="utf-8"
    )
    settings = SettingsStore(portable_paths).load()
    assert settings.ancs_adapter == ""
    assert any("ancs_adapter" in warning for warning in settings.warnings)


# --------------------------------------------------------------------------- #
# Status text and attribute mapping
# --------------------------------------------------------------------------- #
def test_describe_ancs_state_covers_every_status():
    assert "Off" in describe_ancs_state(AncsState())
    assert "Opening" in describe_ancs_state(
        AncsState(status=AncsStatus.STARTING, detail="Opening the USB adapter…")
    )
    assert "Settings → Bluetooth" in describe_ancs_state(
        AncsState(status=AncsStatus.ADVERTISING)
    )
    assert "Pixel" in describe_ancs_state(
        AncsState(status=AncsStatus.CONNECTING, device_name="Pixel")
    )
    assert "Receiving notifications from iPhone" in describe_ancs_state(
        AncsState(status=AncsStatus.CONNECTED, device_name="iPhone")
    )
    assert "error: driver" in describe_ancs_state(
        AncsState(status=AncsStatus.ERROR, error="driver")
    )


class _AttributeId(IntEnum):
    APP_IDENTIFIER = 0
    TITLE = 1
    SUBTITLE = 2
    MESSAGE = 3
    DATE = 5


def test_attributes_map_onto_the_stored_notification_fields():
    mapped = notification_from_attributes(
        uid=7,
        category="EMAIL",
        app_name="Mail",
        attributes=[
            (_AttributeId.APP_IDENTIFIER, "com.apple.mobilemail"),
            (_AttributeId.TITLE, "Quarterly report"),
            (_AttributeId.SUBTITLE, "Finance"),
            (_AttributeId.MESSAGE, "Numbers are attached."),
        ],
    )
    assert mapped.uid == 7
    assert mapped.app_name == "Mail"
    assert mapped.title == "Quarterly report"
    assert mapped.subtitle == "Finance"
    assert mapped.body == "Numbers are attached."
    assert mapped.remote_id == "ancs-7-0"


def test_notification_payload_goes_through_the_normal_ingest_path(db):
    ingestor = Ingestor(db)
    notification = sample_notification()

    result = ingestor.handle_payload(
        notification.to_bark_payload(), source_server="ancs"
    )

    assert result.outcome == ParseOutcome.MESSAGE
    stored = db.list_notifications()[0]
    assert stored.title == "Messages - Ada"
    assert stored.subtitle is None
    assert stored.display_title == "Messages - Ada"
    assert stored.body == "Dinner at 8?"
    assert stored.group_name == "Messages"
    assert stored.remote_id == "ancs-42-1700000000000"
    assert stored.source_server == "ancs"

    # A duplicate delivery (iPhone re-notifies) keeps one history row.
    ingestor.handle_payload(notification.to_bark_payload(), source_server="ancs")
    assert db.count_notifications() == 1


def test_payload_falls_back_to_the_app_name_when_attributes_are_empty():
    notification = AncsNotification(uid=1, app_name="Calendar", category="SCHEDULE")
    assert notification.display_title == "Calendar"
    payload = notification.to_bark_payload().decode("utf-8")
    assert "Calendar" in payload


def test_display_title_prefixes_the_app_name_only_when_both_are_known():
    assert (
        AncsNotification(uid=1, app_name="Messages", title="Ada").display_title
        == "Messages - Ada"
    )
    assert AncsNotification(uid=1, title="Ada").display_title == "Ada"
    assert (
        AncsNotification(uid=1, app_name="Messages", title="  ").display_title
        == "Messages"
    )


# --------------------------------------------------------------------------- #
# Candidate selection
# --------------------------------------------------------------------------- #
def test_advertisement_peer_filters_non_connectable_and_non_phone_devices():
    assert advertisement_peer(address="", connectable=True, apple=True) is None
    assert advertisement_peer(address="aa", connectable=False, apple=True) is None
    assert advertisement_peer(address="aa", name="AirPods", apple=True) is None
    assert (
        advertisement_peer(
            address="aa", name="Ada's iPhone", connectable=True, apple=True
        )
        is not None
    )


def test_advertisement_peer_can_include_every_device_for_the_probe():
    android = advertisement_peer(
        address="aa", name="Pixel 8", connectable=True, include_all=True
    )
    assert android is not None
    assert android.name == "Pixel 8"
    assert android.apple is False
    assert android.connectable is True

    beacon = advertisement_peer(
        address="bb", name="", connectable=False, include_all=True
    )
    assert beacon is not None
    assert beacon.connectable is False
    # The default stays iPhone-only, so the receiver's filtering is unchanged.
    assert advertisement_peer(address="aa", name="Pixel 8") is None


# --------------------------------------------------------------------------- #
# Worker state machine
# --------------------------------------------------------------------------- #
def test_worker_advertises_connects_forwards_and_returns_to_advertising():
    settings = Settings(
        ancs_enabled=True,
        ancs_adapter="usb:0A12:0001",
        ancs_adapter_name="CSR8510 A10",
    )
    registry = _BackendRegistry()
    received: list[AncsNotification] = []
    worker = make_worker(settings, registry, received)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is AncsStatus.ADVERTISING)
        backend = registry.latest
        assert backend.started is True
        assert backend.adapter == "usb:0A12:0001"

        backend.deliver_peer("Ada's iPhone")
        assert wait_for(lambda: worker.state().status is AncsStatus.CONNECTED)
        assert worker.state().device_name == "Ada's iPhone"

        backend.emit(sample_notification())
        assert wait_for(lambda: received == [sample_notification()])

        backend.drop_connection()
        assert wait_for(lambda: worker.state().status is AncsStatus.ADVERTISING)
        # The next connection is accepted without restarting the adapter.
        backend.deliver_peer("Ada's iPhone")
        assert wait_for(lambda: worker.state().status is AncsStatus.CONNECTED)
    finally:
        worker.stop()
    assert worker.state().status is AncsStatus.OFF
    assert registry.latest.closed is True


def test_start_failure_is_reported_and_retried():
    settings = Settings(ancs_enabled=True, ancs_adapter="usb:0A12:0001")
    registry = _BackendRegistry(fail_start="adapter busy")
    worker = make_worker(settings, registry)
    try:
        worker.start()
        assert wait_for(
            lambda: worker.state().status is AncsStatus.ERROR
            and "adapter busy" in worker.state().error
        )
    finally:
        worker.stop()
    assert worker.state().status is AncsStatus.OFF


def test_changing_the_adapter_restarts_the_receiver():
    settings = Settings(
        ancs_enabled=True,
        ancs_adapter="usb:1111:0001",
    )
    registry = _BackendRegistry()
    worker = make_worker(settings, registry)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is AncsStatus.ADVERTISING)
        first = registry.latest

        worker.configure(
            Settings(
                ancs_enabled=True,
                ancs_adapter="usb:2222:0002",
            )
        )
        assert wait_for(lambda: len(registry.instances) == 2)
        assert first.closed is True
        assert wait_for(lambda: registry.latest.started is True)
        assert registry.latest.adapter == "usb:2222:0002"
    finally:
        worker.stop()


def test_disabling_the_setting_stops_the_receiver():
    settings = Settings(
        ancs_enabled=True,
        ancs_adapter="usb:1111:0001",
    )
    registry = _BackendRegistry()
    worker = make_worker(settings, registry)
    worker.start()
    assert wait_for(lambda: worker.state().status is AncsStatus.ADVERTISING)

    worker.configure(Settings(ancs_enabled=False))
    assert worker.state().status is AncsStatus.OFF
    assert worker.is_running is False


class _StuckCloseBackend(FakeAncsBackend):
    """A backend whose teardown wedges, like a controller that never answers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.release = threading.Event()

    async def close(self):
        await asyncio.get_running_loop().run_in_executor(None, self.release.wait)
        await super().close()


def test_restart_refuses_to_open_a_second_session_on_a_held_adapter():
    """A wedged teardown must not be followed by a second `open_transport`.

    Reopening the USB adapter while the old session still holds it fails with
    “device busy”, leaves the receiver in ERROR and — the user-visible symptom —
    nothing is advertising, so tapping the accessory on the iPhone fails.
    """
    settings = Settings(ancs_enabled=True, ancs_adapter="usb:0A12:0001")
    registry = _BackendRegistry(backend_class=_StuckCloseBackend)
    worker = make_worker(settings, registry)
    try:
        worker.start()
        assert wait_for(lambda: worker.state().status is AncsStatus.ADVERTISING)

        worker.restart(timeout=0.2)
        assert worker.state().status is AncsStatus.ERROR
        assert "shutting down" in worker.state().error
        # The old session is still registered, so `start` cannot race it.
        assert worker.is_running is True
        assert len(registry.instances) == 1
    finally:
        registry.latest.release.set()
        assert worker.stop() is True
    assert worker.is_running is False
    assert worker.state().status is AncsStatus.OFF
