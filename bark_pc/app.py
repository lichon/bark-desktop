"""Application composition root: wires every layer and owns the lifecycle.

Ownership rules (see `research.md` §R15): Tk owns the main thread, the receiver
runs an anyio loop on its own thread, a small ingest thread does storage work so
acks are never delayed, and image fetches use their own workers.  Cross-thread UI
work always goes through `AppShell.post()`.
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import anyio

from .bark.links import ABANDON_NOTE, LinkManager
from .bark.server_client import BarkServerClient, normalize_server_url, push_to_url
from .bluetooth import (
    AncsNotification,
    AncsState,
    AncsWorker,
    BluetoothSinkWorker,
    SinkState,
    bluetooth_hci_support,
    bluetooth_support,
    list_sink_devices_sync,
    list_usb_bluetooth_adapters,
)
from .core import models
from .core.bus import BoundedEventQueue
from .core.config import (
    DEFAULT_PUSH_TOPIC,
    MESSAGE_ROUTE_BARK,
    MESSAGE_ROUTE_BOTH,
    MESSAGE_ROUTE_REALTIME,
    Paths,
    Settings,
    SettingsStore,
    is_supabase_url,
    migrate_legacy_data,
    resolve_paths,
    validate_supabase_url,
)
from .core.db import Database, RecentPayloadFilter
from .core.errors import BarkError
from .core.logging import build_logger, export_diagnostics
from .core.models import (
    CONNECTION_STATUS_LABELS,
    ConnectionStatus,
    ImageState,
    LinkState,
    Notification,
    PushStatus,
)
from .images.cache import ImageStore
from .images.fetcher import ImageFetcher
from .platform.windows.autostart import WindowsAutostart, repair_decision
from .platform.windows.secrets import get_store
from .platform.windows.session import WindowsSessionState
from .platform.windows.single_instance import SingleInstanceManager
from .platform.windows.toast import WindowsToastNotifier
from .platform.windows.tray import TrayController
from .platform.windows.uri_scheme import WindowsUriHandler, parse_barkpc_uri
from .push import supabase_realtime
from .push.identity import IdentityManager
from .push.ingest import Ingestor
from .push.pypush_client import PypushReceiver
from .push.routing import RouteStore, WebhookDispatcher
from .push.supervisor import PushSupervisor
from .tts import TTS_PREVIEW_TEXT, build_tts_provider, should_speak, speech_text
from .ui.setup_wizard import SetupResult, SetupWizard

logger = logging.getLogger("bark_pc.app")

APP_NAME = "Bark Desktop Client"
MAINTENANCE_TICK_S = 60
RETENTION_INTERVAL_S = 60 * 60
VERIFY_INTERVAL_S = 60 * 60
PURGE_INTERVAL_S = 24 * 60 * 60
SELF_TEST_TIMEOUT_S = 30


class BarkApp:
    """The application object (also the controller used by the UI windows)."""

    def __init__(
        self,
        *,
        app_dir: str | Path | None = None,
        argv: list[str] | None = None,
        paths: Paths | None = None,
        start_ui: bool = True,
    ):
        self.argv = list(argv or [])
        self.paths = paths or resolve_paths(app_dir)
        migrated = migrate_legacy_data(self.paths)
        self.paths.ensure_dirs()

        self.settings_store = SettingsStore(self.paths, logger)
        self.settings: Settings = self.settings_store.load()
        self.log = build_logger(self.paths.log_path, level=self.settings.log_level)
        if migrated:
            self.log.info("moved legacy data folder to %s", self.paths.data_dir)
        self.db = Database(self.paths.db_path, logger=self.log)
        self.route_store = RouteStore(self.paths, self.log)
        self.webhook_dispatcher = WebhookDispatcher(
            self.route_store.routes, logger_=self.log
        )

        self.secret_store = get_store()
        self.identity = IdentityManager(
            store=self.secret_store,
            secret_path=self.paths.identity_path,
            db=self.db,
            receiver=PypushReceiver(),
        )
        self.links = LinkManager(self.db, push_topic=self.settings.push_topic)
        self._migrate_legacy_supabase_link()
        self.image_store = ImageStore(
            self.db, self.paths.images_dir, cache_limit_mb=self.settings.image_cache_mb
        )
        self.fetcher = ImageFetcher(self.image_store)
        self.notifier = WindowsToastNotifier(
            app_name=APP_NAME,
            toasts_enabled=self.settings.toasts_enabled,
            hide_preview=self.settings.hide_preview_text,
            image_resolver=self._resolve_image_path,
            on_activated=self._on_toast_activated,
            on_failed=self._on_toast_failed,
        )
        self.single = SingleInstanceManager(self.paths.instance_path, logger_=self.log)
        self.autostart = WindowsAutostart()
        self.session = WindowsSessionState()
        self.uri_handler = WindowsUriHandler()
        self.tts = build_tts_provider(
            self.settings.tts_provider,
            rate=self.settings.tts_rate,
            volume=self.settings.tts_volume,
            log=self.log,
        )
        self._tts_preview = None
        self.bluetooth = BluetoothSinkWorker(self.settings, log=self.log)
        self.ancs = AncsWorker(
            self.settings,
            log=self.log,
            keystore_path=self.paths.data_dir / "bluetooth" / "ancs-keys.json",
            on_notification=self._on_ancs_notification,
        )

        self.events = BoundedEventQueue(maxsize=512)
        self.recent_payloads = RecentPayloadFilter()
        self.ingestor = Ingestor(
            self.db,
            recent_filter=self.recent_payloads,
            on_image_url=self._queue_image,
            on_stored=self._on_notification_stored,
            on_deleted=self._on_notification_deleted,
        )
        self.supervisor: PushSupervisor | None = None
        self.supabase_supervisor: PushSupervisor | None = None
        # Combined tray status: one receiver per transport, both may run at once.
        self._transport_status: dict[str, PushStatus] = {}
        self._transport_reason: dict[str, str] = {}

        self.shell = None  # created by `run()` (needs the main thread)
        self.tray: TrayController | None = None
        self.history_window = None
        self.settings_dialog = None
        self.routing_dialog = None
        self.setup_wizard = None

        self._stop = threading.Event()
        self._ingest_thread: threading.Thread | None = None
        self._maintenance_thread: threading.Thread | None = None
        self._shutdown_done = False
        self._selftest_waiters: dict[str, threading.Event] = {}
        self._selftest_lock = threading.Lock()
        self.start_ui = start_ui
        self._initial_uri: str | None = None
        # Windows refused a toast; kept for the tray tooltip and the history bar.
        self.toast_failure: str | None = None

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def run(self, initial_uri: str | None = None) -> int:
        self._initial_uri = initial_uri or self._uri_from_argv()
        self.log.info("starting from %s (data: %s)", self.paths.app_dir, self.paths.data_dir)

        if not self.single.acquire():
            return self._forward_to_existing()

        try:
            self.single.start_listener(self._handle_ipc_command)
        except OSError as exc:  # pragma: no cover - port exhaustion
            self.log.warning("could not start the instance listener: %s", exc)

        if self.start_ui:
            from .ui.app_shell import AppShell

            self.shell = AppShell(on_close_to_tray=self._on_close_to_tray)
            self.shell.create()
            self._start_tray()

        self.fetcher.start()
        self._start_ingest()
        self._start_maintenance()
        self._prune_now()
        if self.settings.bluetooth_enabled:
            self.bluetooth.start()
        if self.settings.ancs_enabled:
            self.ancs.start()

        pending = self._pending_uri_action()
        if self._needs_setup():
            self._set_status(ConnectionStatus.INVALID, "Setup required")
            if self.start_ui:
                self.shell.post(self.open_setup)
            if pending is not None:
                self.shell.post(pending) if self.shell else pending()
        else:
            self.start_receiver()
            if self.shell is not None:
                # A URI (toast activation) wins; otherwise land on the history
                # window so launches after the first run show something.  The
                # app still keeps running in the tray when the window is closed.
                self.shell.post(pending if pending is not None else self.open_history)

        if self.start_ui:
            self.shell.post(self._post_startup_checks)
            self.shell.run()

        self.shutdown()
        return 0

    def _forward_to_existing(self) -> int:
        uri = self._uri_from_argv()
        command, notification_id = "focus", None
        if uri:
            parsed = parse_barkpc_uri(uri)
            command = "setup" if parsed["command"] == "setup" else "open"
            notification_id = parsed.get("notification_id")
        if self.single.focus_existing(command, notification_id):
            self.log.info("forwarded %s to the running instance", command)
            return 0
        self.log.info("no running instance answered; starting normally")
        return 0

    def _uri_from_argv(self) -> str | None:
        for argument in self.argv:
            if isinstance(argument, str) and argument.lower().startswith("barkpc:"):
                return argument
        return None

    def _pending_uri_action(self):
        uri = self._initial_uri
        if not uri:
            return None
        parsed = parse_barkpc_uri(uri)
        if parsed["command"] == "setup":
            return self.open_setup
        if parsed["command"] == "open" and parsed.get("notification_id") is not None:
            notification_id = parsed["notification_id"]
            return lambda: self.open_history(notification_id)
        return self.open_history

    def _post_startup_checks(self) -> None:
        self._maybe_offer_autostart_repair()
        self._refresh_status_summary()

    def _migrate_legacy_supabase_link(self) -> None:
        """Move a pre-dual-transport `sb://` link out of the Bark slot.

        Older builds stored either transport in the single `bark_link` row.
        The Bark link is now always a real Bark server, so a Realtime link is
        preserved in settings (as the optional room) and the Bark slot is
        cleared, which makes setup run again for the required Bark side.
        """
        changes: dict = {}
        if is_supabase_url(self.settings.server_url):
            changes["supabase_enabled"] = True
            changes.setdefault("supabase_url", self.settings.server_url)
            changes["server_url"] = "https://api.day.app"
        link = self.db.get_link()
        if link is not None and link.is_supabase:
            changes.setdefault("supabase_enabled", True)
            if not self.settings.supabase_url:
                changes.setdefault("supabase_url", link.server_url)
            if not self.settings.supabase_key:
                changes.setdefault("supabase_key", link.device_key)
            if not self.settings.supabase_topic or self.settings.supabase_topic == "bark":
                changes.setdefault("supabase_topic", link.push_topic or "bark")
            self.db.upsert_link(
                models.BarkLink(server_url="", device_key="", state=LinkState.UNLINKED)
            )
            self.log.info("moved the legacy Supabase link to the optional Realtime room")
        if changes:
            self.settings_store.update(**changes)
            self.settings = self.settings_store.settings

    def _maybe_offer_autostart_repair(self) -> None:
        if not self.settings.autostart_enabled:
            return
        decision = repair_decision(
            enabled=True,
            stored_path=self.settings.autostart_path,
            current_exe=self._exe_path(),
        )
        if decision != "repair":
            return
        from tkinter import messagebox

        answer = messagebox.askyesno(
            "Repair start-with-Windows",
            "The app folder moved, so the start-with-Windows entry points at an old "
            "location.\n\nRepair it now?",
            parent=self.shell.root if self.shell else None,
        )
        if answer:
            self.apply_autostart(True)
        else:
            self.apply_autostart(False)

    # ------------------------------------------------------------------ #
    # Tray + status
    # ------------------------------------------------------------------ #
    def _start_tray(self) -> None:
        self.tray = TrayController(
            actions={
                "open_history": lambda: self._on_ui(self.open_history),
                "test_notification": lambda: self._on_ui(self.send_test_notification),
                "toggle_tts": lambda: self._on_ui(self.toggle_tts),
                "copy_key": lambda: self._on_ui(self.copy_device_key),
                "settings": lambda: self._on_ui(self.open_settings),
                "routing": lambda: self._on_ui(self.open_routing),
                "setup": lambda: self._on_ui(self.open_setup),
                "show_log_folder": lambda: self._on_ui(self.show_log_folder),
                "quit": lambda: self._on_ui(self.request_quit),
            },
            summary=self._tray_summary,
            app_name=APP_NAME,
            checks={"toggle_tts": lambda: bool(self.settings.tts_enabled)},
        )
        self.tray.start()
        self.tray.set_unread(self.db.unread_count())

    def _on_ui(self, func) -> None:
        if self.shell is not None:
            self.shell.post(func)
        else:  # pragma: no cover - headless
            func()

    def _tray_summary(self) -> str:
        link = self.db.get_link()
        server = link.server_url if link and link.server_url else "not linked"
        status = self.tray.status.value if self.tray else "starting"
        summary = f"{server} · {status}"
        return summary

    def _toast_notice(self) -> str:
        """Short suffix for the history toolbar (`HistoryWindow` callback)."""
        return "toasts blocked" if self.toast_failure else ""

    def _refresh_status_summary(self) -> None:
        if self.tray is not None:
            self.tray.set_unread(self.db.unread_count())
        if self.history_window is not None:
            self.history_window.refresh_status()

    def _connection_status_text(self) -> str:
        """Human label for the history toolbar."""
        if self.is_silenced():
            status = ConnectionStatus.SILENCED
        else:
            try:
                status = ConnectionStatus(getattr(self.tray, "status", None))
            except (TypeError, ValueError):
                status = ConnectionStatus.STARTING
        return CONNECTION_STATUS_LABELS.get(status, status.value)

    def _set_status(self, status: ConnectionStatus, reason: str | None = None) -> None:
        if status != ConnectionStatus.SILENCED and self.is_silenced():
            if self.tray is not None:
                self.tray.set_silenced(True)
            self._refresh_status_summary()
            return
        if self.tray is not None:
            self.tray.set_status(status, reason)
        self._refresh_status_summary()

    def _on_status(self, status: PushStatus, reason: str | None, role: str = "bark") -> None:
        def apply() -> None:
            self._publish_status(role, status, reason)
            if status == PushStatus.ACTIVE:
                self.settings_store.set_last_connected()
                self.settings = self.settings_store.settings
                if role == "bark":
                    self.identity.mark_active()
            if status in (PushStatus.INVALID, PushStatus.DEGRADED):
                self.log.info("%s receiver %s: %s", role, status.value, reason or "")

        self._on_ui(apply)

    def _publish_status(
        self, role: str, status: PushStatus, reason: str | None = None
    ) -> None:
        """Record one transport's status and show the combined state in the tray."""
        if status == PushStatus.STOPPED:
            self._transport_status.pop(role, None)
            self._transport_reason.pop(role, None)
        else:
            self._transport_status[role] = status
            self._transport_reason[role] = reason or ""
        combined = self._combined_status()
        mapping = {
            PushStatus.CONNECTING: ConnectionStatus.CONNECTING,
            PushStatus.ACTIVE: ConnectionStatus.ACTIVE,
            PushStatus.DEGRADED: ConnectionStatus.DEGRADED,
            PushStatus.INVALID: ConnectionStatus.INVALID,
            PushStatus.STOPPED: ConnectionStatus.STARTING,
        }
        combined_reason = None
        if combined != PushStatus.ACTIVE:
            combined_reason = next(
                (text for text in self._transport_reason.values() if text), None
            )
        self._set_status(mapping.get(combined, ConnectionStatus.STARTING), combined_reason)

    def _combined_status(self) -> PushStatus:
        """One tray state for the transports that are supposed to run.

        Both receivers run concurrently.  A working connection keeps delivery
        going, but a second transport that needs action still surfaces as
        `degraded` instead of being hidden by the healthy one.
        """
        statuses = list(self._transport_status.values())
        if not statuses:
            return PushStatus.STOPPED
        if all(status == PushStatus.ACTIVE for status in statuses):
            return PushStatus.ACTIVE
        if all(status == PushStatus.INVALID for status in statuses):
            return PushStatus.INVALID
        if any(status == PushStatus.INVALID for status in statuses):
            return PushStatus.DEGRADED
        if any(status == PushStatus.CONNECTING for status in statuses):
            return PushStatus.CONNECTING
        if any(status == PushStatus.ACTIVE for status in statuses):
            return PushStatus.ACTIVE
        return PushStatus.DEGRADED

    def _on_close_to_tray(self) -> None:
        if self.tray is not None:
            self.tray.notify(APP_NAME, "Still running in the tray. Double-click to open.")

    # ------------------------------------------------------------------ #
    # Receiver wiring
    # ------------------------------------------------------------------ #
    def _material(self):
        try:
            return self.identity.load()
        except Exception as exc:
            self.log.warning("identity unavailable: %s", exc)
            self._on_ui(lambda: self.open_setup())
            return None

    def _supabase_config(self) -> tuple[str, str, str] | None:
        """`(server_url, auth_key, room)` for the optional Realtime transport."""
        if not self.settings.supabase_enabled:
            return None
        server = (self.settings.supabase_url or "").strip()
        key = (self.settings.supabase_key or "").strip()
        room = (self.settings.supabase_topic or "").strip() or "bark"
        if not server or not key:
            return None
        return server, key, room

    def start_receiver(self) -> None:
        """Run the Bark receiver and, when enabled, the Realtime receiver too."""
        self._start_bark_receiver()
        self._start_supabase_receiver()

    def _start_bark_receiver(self) -> None:
        if self.supervisor is not None and self.supervisor.is_running:
            return
        link = self.db.get_link()
        if link is None or link.is_supabase or not link.device_key:
            self.log.info("Bark receiver not started: no usable Bark link yet")
            return
        topic = link.push_topic or self.settings.push_topic or DEFAULT_PUSH_TOPIC
        self.supervisor = PushSupervisor(
            PypushReceiver(),
            material_getter=self._material,
            topic=topic,
            on_message=lambda raw: self._on_payload(raw, "bark"),
            on_status=lambda status, reason: self._on_status(status, reason, "bark"),
            on_token=self.identity.update_token,
            on_invalidate_token=self.identity.invalidate_token,
            on_reactivate=self._reactivate_identity,
        )
        self.supervisor.start()
        self.log.info("Bark receiver started (topic %s)", topic)

    def _start_supabase_receiver(self) -> None:
        if self.supabase_supervisor is not None and self.supabase_supervisor.is_running:
            return
        if not self.settings.supabase_enabled:
            return
        config = self._supabase_config()
        if config is None:
            message = (
                "Supabase Realtime is enabled but the project address or auth key is "
                "missing (Settings → Supabase Realtime)."
            )
            self.log.warning("Supabase receiver not started: the project or key is missing")
            self._publish_status("supabase", PushStatus.INVALID, message)
            return
        server, key, topic = config
        try:
            receiver = supabase_realtime.SupabaseRealtimeReceiver(
                server_url=server, auth_key=key, room_name=topic
            )
        except ValueError as exc:
            self.log.warning("Supabase receiver not started: %s", exc)
            self._publish_status("supabase", PushStatus.INVALID, str(exc))
            return
        self.supabase_supervisor = PushSupervisor(
            receiver,
            material_getter=lambda: None,
            topic=topic,
            on_message=lambda raw: self._on_payload(raw, "supabase"),
            on_status=lambda status, reason: self._on_status(status, reason, "supabase"),
        )
        self.supabase_supervisor.start()
        self.log.info("Supabase receiver started (room %s)", topic)

    def stop_receiver(self, timeout: float = 2.0) -> None:
        """Cancel both connections and wait briefly; the threads are daemons."""
        if self.supervisor is not None:
            self.supervisor.stop(timeout=timeout)
            self.supervisor = None
        if self.supabase_supervisor is not None:
            self.supabase_supervisor.stop(timeout=timeout)
            self.supabase_supervisor = None

    async def _reactivate_identity(self) -> None:
        material = await self.identity.activate()
        await self.identity.fetch_base_token(material)

    # ------------------------------------------------------------------ #
    # Ingest (receiver thread → storage thread → UI thread)
    # ------------------------------------------------------------------ #
    def _on_payload(self, raw: bytes, role: str = "bark") -> None:
        """Called on a receiver thread: enqueue only, never block."""
        self.events.put(("payload", (raw, role)))

    def _on_ancs_notification(self, notification: AncsNotification) -> None:
        """Called on the ANCS receiver thread: convert and enqueue only."""
        try:
            raw = notification.to_bark_payload()
        except Exception:  # pragma: no cover - malformed iPhone attributes
            self.log.exception("ancs: could not build a payload")
            return
        self.events.put(("payload", (raw, "ancs")))

    def _start_ingest(self) -> None:
        if self._ingest_thread is not None:
            return
        self._ingest_thread = threading.Thread(
            target=self._ingest_loop, name="bark-ingest", daemon=True
        )
        self._ingest_thread.start()

    def _ingest_loop(self) -> None:
        while not self._stop.is_set():
            try:
                kind, payload = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if kind == "payload":
                    raw, role = payload
                    self._ingest_payload(raw, role)
            except Exception:  # pragma: no cover - never kill the ingest thread
                self.log.exception("ingest failed")

    def _ingest_payload(self, raw: bytes, role: str = "bark") -> None:
        if role == "ancs":
            # Keep ANCS history separate from Bark servers: the same numeric
            # notification UID may exist on another transport.
            host = "ancs"
        elif role == "supabase":
            config = self._supabase_config()
            server = config[0] if config else self.settings.supabase_url
            host = urlsplit(server).hostname or server
        else:
            link = self.db.get_link()
            server = (link.server_url if link and link.server_url else "") or (
                self.settings.server_url
            )
            host = urlsplit(server).hostname or server
        try:
            self.ingestor.handle_payload(raw, source_server=host)
        except Exception:  # pragma: no cover - never kill the ingest thread
            self.log.exception("ingest failed")

    # -- ingest callbacks -------------------------------------------------- #
    def _queue_image(self, notification: Notification) -> None:
        if not notification.image_url:
            return
        self.fetcher.submit(
            notification.image_url,
            priority=10,
            callback=lambda entry: self._on_image_ready(notification, entry),
        )

    def _on_notification_stored(self, notification: Notification) -> None:
        self._signal_selftest(notification.remote_id)
        if not notification.from_self:
            try:
                self.webhook_dispatcher.submit(notification)
            except Exception:  # pragma: no cover - routing must never break ingest
                self.log.exception("webhook routing failed")
        self._on_ui(lambda: self._present(notification))

    def _on_notification_deleted(self, remote_id: str) -> None:
        self._on_ui(lambda: self._dismiss_toast(remote_id))

    def _present(self, notification: Notification) -> None:
        """UI thread: toast + speech + badge + history refresh."""
        if notification.from_self:
            self.log.debug("own message: stored as read, no toast or speech")
        else:
            try:
                if self.is_silenced():
                    self.log.debug("silenced: not showing a toast")
                elif self.settings.toasts_enabled:
                    self.notifier.show(notification)
            except Exception:  # pragma: no cover - toast failures must not break the app
                self.log.exception("toast presentation failed")
            self._speak(notification)
        self._refresh_status_summary()
        window = self.history_window
        if window is not None:
            try:
                window.refresh()
            except Exception:  # pragma: no cover
                self.log.exception("history refresh failed")

    def _speak(self, notification: Notification) -> None:
        """UI thread: read the notification aloud when the user asked for it."""
        enabled = bool(self.settings.tts_enabled)
        silenced = self.is_silenced()
        only_when_unlocked = bool(self.settings.tts_only_when_unlocked)
        locked = only_when_unlocked and self.is_screen_locked()
        if not should_speak(
            enabled=enabled,
            silenced=silenced,
            only_when_unlocked=only_when_unlocked,
            locked=locked,
        ):
            if locked and enabled and not silenced:
                self.log.debug("screen is locked: not reading the notification aloud")
            return
        text = speech_text(
            notification,
            include_body=self.settings.tts_include_body,
            hide_preview=self.settings.hide_preview_text,
            max_chars=self.settings.tts_max_chars,
        )
        self._speak_line(text)

    def is_screen_locked(self) -> bool:
        """Whether the workstation is locked; a probe failure means "unlocked"."""
        try:
            return bool(self.session.is_locked())
        except Exception:  # pragma: no cover - the probe must never mute speech
            self.log.debug("screen lock probe failed", exc_info=True)
            return False

    def _speak_line(self, text: str) -> None:
        try:
            if not self.tts.speak(text):
                self.log.debug("speech provider %s did not accept the line", self.tts.name)
        except Exception:  # pragma: no cover - speech must never break delivery
            self.log.exception("text-to-speech failed")

    def _dismiss_toast(self, remote_id: str) -> None:
        try:
            self.notifier.remove(Notification(remote_id=remote_id))
        except Exception:  # pragma: no cover
            self.log.exception("toast dismissal failed")
        self._refresh_status_summary()
        if self.history_window is not None:
            self.history_window.refresh()

    def _on_image_ready(self, notification: Notification, entry) -> None:
        def apply() -> None:
            if self.history_window is not None:
                self.history_window.refresh()
            if (
                entry is not None
                and entry.state == ImageState.CACHED
                and not notification.from_self
                and self.settings.toasts_enabled
                and not self.is_silenced()
                and notification.received_at + 30_000 > models.now_ms()
            ):
                try:
                    self.notifier.remove(notification)
                    self.notifier.show(notification)
                except Exception:  # pragma: no cover
                    self.log.exception("hero toast update failed")

        self._on_ui(apply)

    def _resolve_image_path(self, notification: Notification) -> Path | None:
        if not notification.image_url:
            return None
        entry = self.image_store.get(notification.image_url)
        if entry is None or entry.state != ImageState.CACHED:
            return None
        return self.image_store.file_path(entry)

    def _on_toast_activated(self, payload: dict) -> None:
        # A toasted click proves presentation works again after a failure.
        self.toast_failure = None
        self._on_ui(self.open_history)

    def _on_toast_failed(self, message: str) -> None:
        """Windows refused a toast (called from the WinRT event thread).

        `bark_pc.toast` already logged the HRESULT; this keeps the reason in
        front of the user instead of letting a broken notification path look
        like a broken push path.
        """
        if message == self.toast_failure:
            return
        self.toast_failure = message
        self._on_ui(self._refresh_status_summary)

    # ------------------------------------------------------------------ #
    # Setup flow (called from the wizard's worker thread)
    # ------------------------------------------------------------------ #
    def run_setup(
        self,
        server_url: str,
        progress=None,
        *,
        supabase_enabled: bool | None = None,
        supabase_url: str | None = None,
        supabase_key: str | None = None,
        room_name: str | None = None,
    ) -> SetupResult:
        report = progress or (lambda _message: None)

        def step(message: str) -> None:
            self.log.info("setup: %s", message.rstrip("…"))
            report(message)

        normalized = normalize_server_url(server_url)
        if is_supabase_url(normalized):
            raise BarkError(
                "Enter a Bark server address (https://…). Supabase Realtime is optional: "
                "tick the Realtime box to receive from a room as well."
            )
        self.log.info("setup: starting (server=%s)", normalized)

        # The optional Realtime transport is checked first so a bad project or
        # key never leaves the required Bark side half-configured.
        enable_supabase = (
            self.settings.supabase_enabled if supabase_enabled is None else bool(supabase_enabled)
        )
        sb_url = ""
        sb_key = ""
        sb_room = ""
        if enable_supabase:
            try:
                sb_url = validate_supabase_url(
                    supabase_url if supabase_url is not None else self.settings.supabase_url
                )
            except ValueError as exc:
                raise BarkError(f"The Supabase project address is invalid: {exc}") from exc
            if not sb_url:
                raise BarkError(
                    "Enter the Supabase project URL (https://project.supabase.co)."
                )
            sb_key = (
                supabase_key if supabase_key is not None else self.settings.supabase_key
            ).strip()
            if not sb_key:
                raise BarkError(
                    "Enter the Supabase auth key (the project's anon or publishable key)."
                )
            sb_room = (room_name or self.settings.supabase_topic or "bark").strip()
            step("Checking the Supabase project…")
            supabase_realtime.ping(sb_url, sb_key)

        step("Activating this device anonymously…")
        material = anyio.run(lambda: self.identity.activate())
        step("Connecting to the push service…")
        token = anyio.run(lambda: self.identity.fetch_base_token(material))
        step("Registering with the server…")
        link = self.links.register(normalized, token, push_topic=self.settings.push_topic)
        changes: dict = {"server_url": link.server_url, "supabase_enabled": enable_supabase}
        if enable_supabase:
            changes.update(
                supabase_url=sb_url,
                supabase_key=sb_key,
                supabase_topic=sb_room,
            )
        self.settings_store.update(**changes)
        self.settings = self.settings_store.settings
        self._register_uri_handler()
        step("Starting the receivers…")
        self.stop_receiver()
        self.start_receiver()
        self._on_ui(lambda: self._set_status(ConnectionStatus.CONNECTING))
        self.log.info(
            "setup: finished (server=%s%s)",
            link.server_url,
            f", Supabase room {sb_room}" if enable_supabase else "",
        )
        return SetupResult(
            ok=True,
            server_url=link.server_url,
            device_key=link.device_key,
            push_url=link.push_url,
            supabase_enabled=enable_supabase,
            supabase_url=sb_url if enable_supabase else "",
            supabase_room=sb_room if enable_supabase else "",
        )

    def _register_uri_handler(self) -> None:
        try:
            self.uri_handler.register(self._exe_path())
        except Exception as exc:  # pragma: no cover - registry permissions
            self.log.warning("could not register the barkpc: URI scheme: %s", exc)

    def run_self_test(self) -> tuple[bool, bool, str]:
        """Push a test notification through the Bark server only.

        The optional Supabase Realtime room is deliberately left out: it is an
        extra transport, not the server this device is linked to, and a room
        broadcast would make one test look like two notifications.
        """
        link = self.db.get_link()
        if link is None or not link.device_key or link.is_supabase:
            return False, False, "No device key yet. Complete setup first."
        remote_id = str(uuid.uuid4())
        waiter = threading.Event()
        with self._selftest_lock:
            self._selftest_waiters[remote_id] = waiter
        errors: list[str] = []
        sent = False
        try:
            client = BarkServerClient(link.server_url)
            try:
                client.push_test(link.device_key, message_id=remote_id)
                sent = True
            finally:
                client.close()
        except BarkError as exc:
            errors.append(f"Bark server: {exc}")
        if not sent:
            with self._selftest_lock:
                self._selftest_waiters.pop(remote_id, None)
            return False, False, "; ".join(errors)
        received = waiter.wait(SELF_TEST_TIMEOUT_S)
        with self._selftest_lock:
            self._selftest_waiters.pop(remote_id, None)
        return True, received, "; ".join(errors)

    def _signal_selftest(self, remote_id: str | None) -> None:
        if not remote_id:
            return
        with self._selftest_lock:
            waiter = self._selftest_waiters.get(remote_id)
        if waiter is not None:
            waiter.set()

    def check_connection_async(self, done) -> None:
        def worker() -> None:
            notes: list[str] = []
            problems: list[str] = []
            link = self.db.get_link()
            if link is not None and link.server_url and not link.is_supabase:
                client = BarkServerClient(link.server_url)
                try:
                    client.ping()
                    host = urlsplit(link.server_url).hostname or link.server_url
                    notes.append(f"Bark: {host} ok")
                except BarkError as exc:
                    problems.append(f"Bark: {exc}")
                finally:
                    client.close()
            else:
                problems.append("Bark server: not configured")
            message = "; ".join(notes + problems)
            done(not problems, message)

        threading.Thread(target=worker, name="bark-check", daemon=True).start()

    # ------------------------------------------------------------------ #
    # Settings application
    # ------------------------------------------------------------------ #
    def apply_settings(self, settings: Settings) -> None:
        previous_connection = (
            self.settings.server_url,
            self.settings.push_topic,
            self.settings.supabase_enabled,
            self.settings.supabase_url,
            self.settings.supabase_key,
            self.settings.supabase_topic,
        )
        previous_tts = (
            self.settings.tts_provider,
            self.settings.tts_rate,
            self.settings.tts_volume,
        )
        previous_bluetooth = (
            self.settings.bluetooth_enabled,
            self.settings.bluetooth_device_id,
            self.settings.bluetooth_device_name,
        )
        previous_ancs = (
            self.settings.ancs_enabled,
            self.settings.ancs_adapter,
            self.settings.ancs_adapter_name,
        )
        previous_log_level = self.settings.log_level
        self.settings = settings
        if previous_log_level != settings.log_level:
            self.log = build_logger(self.paths.log_path, level=settings.log_level)
            self.log.info("log level changed to %s", settings.log_level)
        self.image_store.cache_limit_mb = settings.image_cache_mb
        self.notifier.toasts_enabled = settings.toasts_enabled
        self.notifier.hide_preview = settings.hide_preview_text
        if previous_tts != (settings.tts_provider, settings.tts_rate, settings.tts_volume):
            self._rebuild_tts()
        if settings.autostart_enabled != self.autostart.is_enabled():
            self.apply_autostart(settings.autostart_enabled)
        if previous_bluetooth != (
            settings.bluetooth_enabled,
            settings.bluetooth_device_id,
            settings.bluetooth_device_name,
        ):
            self.bluetooth.configure(settings)
        if previous_ancs != (
            settings.ancs_enabled,
            settings.ancs_adapter,
            settings.ancs_adapter_name,
        ):
            self.ancs.configure(settings)
        if settings.silence_until and settings.silence_until > models.now_ms():
            if self.tray is not None:
                self.tray.set_silenced(True)
            try:
                self.tts.stop()
            except Exception:  # pragma: no cover - speech must never block settings
                self.log.exception("could not stop speaking")
        elif self.tray is not None:
            self.tray.set_silenced(False)
            if PushStatus.ACTIVE in self._transport_status.values():
                self.tray.set_status(ConnectionStatus.ACTIVE)
        if previous_connection != (
            settings.server_url,
            settings.push_topic,
            settings.supabase_enabled,
            settings.supabase_url,
            settings.supabase_key,
            settings.supabase_topic,
        ):
            self.log.info("receiver settings changed; restarting the receivers")
            self.stop_receiver()
            self.start_receiver()
        self.image_store.evict()
        if self.history_window is not None:
            # The compose bar depends on the message route, not only on the
            # receiver status, so re-evaluate its Send button after a save.
            self.history_window.refresh_status()

    def _rebuild_tts(self) -> None:
        """Recreate the speech provider after its settings changed."""
        try:
            self.tts.shutdown(timeout=1.0)
        except Exception:  # pragma: no cover - defensive
            self.log.exception("could not stop the speech provider")
        self.tts = build_tts_provider(
            self.settings.tts_provider,
            rate=self.settings.tts_rate,
            volume=self.settings.tts_volume,
            log=self.log,
        )
        self.log.info("text-to-speech provider: %s", self.tts.name)

    def preview_tts(
        self, *, provider: str | None = None, rate: int | None = None,
        volume: int | None = None, text: str = TTS_PREVIEW_TEXT,
    ) -> bool:
        """Speak a sample line using the dialog's (possibly unsaved) settings."""
        if self._tts_preview is not None:
            try:
                self._tts_preview.shutdown(timeout=0.5)
            except Exception:  # pragma: no cover - defensive
                pass
        self._tts_preview = build_tts_provider(
            provider or self.settings.tts_provider,
            rate=self.settings.tts_rate if rate is None else rate,
            volume=self.settings.tts_volume if volume is None else volume,
            log=self.log,
        )
        return self._tts_preview.speak(text)

    def toggle_tts(self) -> None:
        """Tray action: turn spoken notifications on or off."""
        enabled = not self.settings.tts_enabled
        self.settings_store.update(tts_enabled=enabled)
        self.apply_settings(self.settings_store.settings)
        if enabled:
            self._speak_line(TTS_PREVIEW_TEXT)
        self._refresh_status_summary()

    def apply_autostart(self, enabled: bool) -> None:
        try:
            if enabled:
                self.autostart.enable(self._exe_path())
                self.settings_store.update(
                    autostart_enabled=True,
                    autostart_path=str(self._exe_path()),
                    launch_at_login_configured_at=models.now_ms(),
                )
            else:
                self.autostart.disable()
                self.settings_store.update(autostart_enabled=False, autostart_path=None)
            self.settings = self.settings_store.settings
        except (OSError, ValueError) as exc:
            self.log.warning("could not change the autostart entry: %s", exc)

    def is_silenced(self) -> bool:
        until = self.settings.silence_until
        return bool(until and until > models.now_ms())

    def clear_history(self) -> int:
        removed = self.db.clear_history()
        self.image_store.purge_orphans(max_age_days=0)
        if self.history_window is not None:
            self.history_window.refresh()
        self._refresh_status_summary()
        return removed

    def export_diagnostics(self) -> Path | None:
        from tkinter import filedialog

        parent = self.shell.root if self.shell else None
        target = filedialog.askdirectory(title="Choose a folder for diagnostics", parent=parent)
        if not target:
            return None
        path = export_diagnostics(self.paths.logs_dir, self.settings.to_dict(), Path(target))
        self.log.info("diagnostics exported to %s", path)
        return path

    def remove_all_data(self) -> str:
        removed: list[str] = []
        self.stop_receiver()
        self.ancs.stop(timeout=2.0)
        try:
            self.autostart.disable()
            removed.append("autostart entry")
        except OSError:  # pragma: no cover
            pass
        try:
            self.uri_handler.unregister()
            removed.append("barkpc: registration")
        except OSError:  # pragma: no cover
            pass
        self.single.release()
        self.db.close()
        targets = [
            self.paths.identity_path,
            self.paths.db_path,
            Path(f"{self.paths.db_path}-wal"),
            Path(f"{self.paths.db_path}-shm"),
            self.paths.settings_path,
            self.paths.routes_path,
            self.paths.instance_path,
            self.paths.data_dir / "bluetooth" / "ancs-keys.json",
        ]
        for path in targets:
            try:
                path.unlink(missing_ok=True)
                removed.append(path.name)
            except OSError:  # pragma: no cover
                pass
        for folder in (self.paths.images_dir, self.paths.logs_dir):
            try:
                for child in folder.glob("*"):
                    child.unlink(missing_ok=True)
                removed.append(f"{folder.name}/")
            except OSError:  # pragma: no cover
                pass
        self.log.info("removed: %s", ", ".join(removed))
        self._on_ui(self._quit_after_wipe)
        return "Removed: " + ", ".join(removed)

    def _quit_after_wipe(self) -> None:
        if self.shell is not None:
            self.shell.stop()
        else:  # pragma: no cover - headless
            self.shutdown()

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #
    def _start_maintenance(self) -> None:
        if self._maintenance_thread is not None:
            return
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="bark-maintenance", daemon=True
        )
        self._maintenance_thread.start()

    def _prune_now(self) -> None:
        try:
            counts = self.db.prune(
                self.settings.history_retention_count,
                self.settings.history_retention_days,
            )
            if counts["by_age"] or counts["by_count"]:
                self.log.info(
                    "retention removed %s by age and %s by count",
                    counts["by_age"],
                    counts["by_count"],
                )
        except Exception:  # pragma: no cover - maintenance must never crash startup
            self.log.exception("retention failed")

    def _maintenance_loop(self) -> None:
        last_prune = last_verify = last_purge = time.monotonic()
        while not self._stop.wait(MAINTENANCE_TICK_S):
            now = time.monotonic()
            try:
                if now - last_prune >= RETENTION_INTERVAL_S:
                    last_prune = now
                    self._prune_now()
                    self._expire_silence()
                if now - last_verify >= VERIFY_INTERVAL_S:
                    last_verify = now
                    self._verify_link()
                if now - last_purge >= PURGE_INTERVAL_S:
                    last_purge = now
                    self.image_store.purge_orphans()
                    self.image_store.evict()
            except Exception:  # pragma: no cover - defensive
                self.log.exception("maintenance cycle failed")

    def _expire_silence(self) -> None:
        until = self.settings.silence_until
        if until and until <= models.now_ms():
            self.settings_store.update(silence_until=None)
            self.settings = self.settings_store.settings
            self._on_ui(lambda: self._set_status(ConnectionStatus.ACTIVE))

    def _verify_link(self) -> None:
        link = self.db.get_link()
        if link is None or link.state != LinkState.LINKED:
            return
        try:
            self.links.verify(link)
            if self.history_window is not None:
                self._refresh_status_summary()
        except BarkError as exc:
            self.log.info("link verification failed: %s", exc)

    # ------------------------------------------------------------------ #
    # UI surfaces
    # ------------------------------------------------------------------ #
    def open_history(self, notification_id: int | None = None) -> None:
        if self.shell is None:
            return
        from .ui.history_window import HistoryWindow

        if self.history_window is None:
            self.history_window = HistoryWindow(
                self.shell.root,
                db=self.db,
                factor=self.shell.factor,
                image_store=self.image_store,
                callbacks={
                    "on_read": lambda _id: self._refresh_status_summary(),
                    "on_open_settings": self.open_settings,
                    "on_open_routing": self.open_routing,
                    "on_clear_all": self.clear_history,
                    "connection_status": self._connection_status_text,
                    "can_send_message": self.can_send_message,
                    "on_send_message": self.send_message,
                    "on_copy_text": self.copy_text,
                    "on_retry_image": self.retry_image,
                    "toast_notice": self._toast_notice,
                },
            )
        self.history_window.show()
        if notification_id is not None:
            self.history_window.select_notification(notification_id)

    def open_settings(self) -> None:
        if self.shell is None:
            return
        from .ui.settings_dialog import SettingsDialog

        if self.settings_dialog is not None:
            try:
                self.settings_dialog.show()
                return
            except Exception:  # pragma: no cover - stale window
                self.settings_dialog = None
        self.settings_dialog = SettingsDialog(
            self.shell.root,
            settings_store=self.settings_store,
            controller=self,
            on_saved=self.apply_settings,
            on_closed=self._on_settings_closed,
        )
        self.settings_dialog.show()

    def _on_settings_closed(self) -> None:
        self.settings_dialog = None

    # ------------------------------------------------------------------ #
    # Bluetooth audio receiver (A2DP sink)
    # ------------------------------------------------------------------ #
    def bluetooth_support(self) -> tuple[bool, str]:
        return bluetooth_support()

    def bluetooth_devices(self):
        """Paired A2DP devices for the settings dialog (may block briefly)."""
        return list_sink_devices_sync()

    def bluetooth_state(self) -> SinkState:
        return self.bluetooth.state()

    def bluetooth_connect(self, device_id: str, device_name: str = "") -> None:
        self.bluetooth.connect_now(device_id, device_name)

    def bluetooth_disconnect(self) -> None:
        self.bluetooth.disconnect_now()

    def open_bluetooth_settings(self) -> None:
        """Open the Windows page where a phone is paired."""
        starter = getattr(os, "startfile", None)
        if starter is None:  # pragma: no cover - non-Windows
            return
        try:
            starter("ms-settings:bluetooth")
        except OSError as exc:  # pragma: no cover - shell association missing
            self.log.warning("could not open Windows Bluetooth settings: %s", exc)

    # ------------------------------------------------------------------ #
    # iPhone notifications (ANCS over a USB Bluetooth adapter)
    # ------------------------------------------------------------------ #
    def ancs_support(self) -> tuple[bool, str]:
        return bluetooth_hci_support()

    def ancs_adapters(self):
        """USB Bluetooth controllers Bumble can use (may block briefly)."""
        return list_usb_bluetooth_adapters()

    def ancs_state(self) -> AncsState:
        return self.ancs.state()

    def ancs_restart(self) -> None:
        self.ancs.reconnect_now()

    def open_routing(self) -> None:
        if self.shell is None:
            return
        from .ui.routing_dialog import RoutingDialog

        if self.routing_dialog is not None:
            try:
                self.routing_dialog.show()
                return
            except Exception:  # pragma: no cover - stale window
                self.routing_dialog = None
        self.routing_dialog = RoutingDialog(
            self.shell.root,
            route_store=self.route_store,
            on_saved=self.apply_webhook_routes,
            on_closed=self._on_routing_closed,
        )
        self.routing_dialog.show()

    def _on_routing_closed(self) -> None:
        self.routing_dialog = None

    def apply_webhook_routes(self, routes) -> None:
        """Apply freshly saved webhook rules without restarting the receivers."""
        self.webhook_dispatcher.set_routes(list(routes))

    def open_setup(self) -> None:
        if self.shell is None:
            return
        if self.setup_wizard is not None:
            try:
                self.setup_wizard.show()
                return
            except Exception:  # pragma: no cover - stale window
                self.setup_wizard = None
        link = self.db.get_link()
        self.setup_wizard = SetupWizard(
            self.shell.root,
            controller=self,
            current_server=link.server_url if link else self.settings.server_url,
            current_topic=(link.push_topic if link else "") or self.settings.push_topic,
            current_supabase_enabled=self.settings.supabase_enabled,
            current_supabase_url=self.settings.supabase_url,
            current_supabase_key=self.settings.supabase_key,
            current_supabase_topic=self.settings.supabase_topic,
            on_finished=self._on_setup_finished,
        )
        self.setup_wizard.show()

    def _on_setup_finished(self, result: SetupResult) -> None:
        self._set_status(ConnectionStatus.CONNECTING)
        self.start_receiver()

    def open_image(self, notification: Notification) -> None:
        if notification is None or not notification.image_url or self.shell is None:
            return
        entry = self.image_store.get(notification.image_url)
        path = self.image_store.file_path(entry) if entry else None
        if path is None:
            self.retry_image(notification.image_url)
            return
        from .ui.image_viewer import ImageViewer

        ImageViewer(self.shell.root, path=path, title=notification.display_title or "Image")

    def retry_image(self, image_url: str | None) -> None:
        if not image_url:
            return
        entry = self.image_store.get(image_url)
        if entry is not None and entry.id is not None:
            self.db.update_image_state(entry.id, ImageState.PENDING, url=image_url)
        self.fetcher.submit(
            image_url,
            priority=20,
            callback=lambda _entry: self._on_ui(
                lambda: self.history_window.refresh() if self.history_window else None
            ),
        )

    def show_log_folder(self) -> None:
        """Open the logs folder so the current and rotated logs are reachable."""
        target = self.paths.logs_dir
        try:
            os.startfile(str(target))  # noqa: S606 - Windows shell open
        except Exception:
            self.log.exception("could not open %s", target)

    def open_url(self, url: str | None) -> None:
        if not url:
            return
        if not url.lower().startswith(("http://", "https://")):
            self.log.warning("refusing to open a non-http(s) URL")
            return
        import webbrowser

        try:
            webbrowser.open(url)
        except Exception:  # pragma: no cover - no default browser
            self.log.exception("could not open the URL")

    def copy_text(self, text: str) -> None:
        if self.shell is None or self.shell.root is None:
            return
        self.shell.root.clipboard_clear()
        self.shell.root.clipboard_append(text or "")

    def copy_device_key(self) -> None:
        link = self.db.get_link()
        if link and link.device_key:
            self.copy_text(link.device_key)
            if self.tray is not None:
                self.tray.notify(APP_NAME, "Device key copied to the clipboard.")

    def send_test_notification(self) -> None:
        """Send a test push through the Bark server (never the Realtime room)."""
        link = self.db.get_link()
        if link is None or not link.device_key or link.is_supabase:
            self._notify_test_failure("No device key yet. Complete setup first.")
            return
        try:
            client = BarkServerClient(link.server_url)
            try:
                client.push_test(link.device_key)
            finally:
                client.close()
        except BarkError as exc:
            self.log.warning("test push failed: %s", exc)
            self._notify_test_failure(str(exc))

    def _notify_test_failure(self, message: str, title: str = "Test notification failed") -> None:
        if self.tray is not None:
            self.tray.notify(title, message)

    def is_supabase_connected(self) -> bool:
        """True while the optional Realtime receiver is active.

        `_message_targets` combines this with the configured self-message route
        and the device-key fallback, so the compose bar's Send button is only
        disabled when nothing at all can deliver a message.
        """
        return self._transport_status.get("supabase") == PushStatus.ACTIVE

    def _self_push_url(self) -> str:
        """The push URL of this very device, or "" when there is none.

        This is the device key registered during setup, turned into a normal
        Bark push URL, and it only counts while the link is still trustworthy
        (`linked` or `stale`).  It is what "send to self" falls back to when no
        *Message send* route has been configured, and it is skipped for
        Supabase links, whose server address is not a Bark server.
        """
        link = self.db.get_link()
        if link is None or link.is_supabase or not link.is_usable:
            return ""
        return link.push_url

    def _message_targets(self) -> tuple[tuple[str, str, str] | None, str, bool]:
        """Resolve where a self message goes right now.

        Returns the Realtime config (or ``None``), a Bark push URL (or ``""``)
        and whether that URL is the device-key fallback.  The configured route
        wins; when it cannot deliver, the message still goes to this device
        through its own device key, so an install that never touched *Message
        send* can send messages too.
        """
        route = self.settings.message_route
        realtime_config = None
        if route in (MESSAGE_ROUTE_REALTIME, MESSAGE_ROUTE_BOTH):
            if self.is_supabase_connected():
                realtime_config = self._supabase_config()
        bark_url = ""
        if route in (MESSAGE_ROUTE_BARK, MESSAGE_ROUTE_BOTH):
            bark_url = (self.settings.message_route_url or "").strip()
        if realtime_config is None and not bark_url:
            bark_url = self._self_push_url()
            return realtime_config, bark_url, bool(bark_url)
        return realtime_config, bark_url, False

    def can_send_message(self) -> bool:
        """True when a compose-bar message can leave this machine somehow."""
        realtime_config, bark_url, _fallback = self._message_targets()
        return bool(realtime_config or bark_url)

    def send_message(self, text: str) -> bool:
        """Send a message this machine wrote through the configured route.

        *Realtime room* keeps the previous behaviour; *Bark URL* POSTs to the
        configured Bark push URL (no Supabase connection needed); *Both* uses
        whichever of the two is available.  When the route cannot deliver, the
        message is pushed to this device's own device key instead.  Network
        work happens on a worker thread so the compose bar never blocks.
        """
        text = (text or "").strip()
        if not text:
            return False
        realtime_config, bark_url, fallback = self._message_targets()
        if realtime_config is None and not bark_url:
            self.log.warning(
                "cannot send self message: the %s route is not available and "
                "this device has no push URL",
                self.settings.message_route,
            )
            return False
        if fallback:
            self.log.info(
                "no message route configured; sending the message to this device"
            )
        threading.Thread(
            target=self._publish_self_message,
            args=(text, realtime_config, bark_url),
            name="bark-self-message",
            daemon=True,
        ).start()
        return True

    def _publish_self_message(
        self,
        text: str,
        realtime_config: tuple[str, str, str] | None,
        bark_url: str,
    ) -> None:
        delivered: list[str] = []
        failures: list[str] = []
        if realtime_config is not None:
            server, key, room = realtime_config
            payload = {"aps": {"alert": {"body": text}}}
            try:
                supabase_realtime.publish_bark_payload(server, key, room, payload)
                delivered.append(f"Realtime room {room}")
            except BarkError as exc:
                failures.append(f"Realtime: {exc}")
        if bark_url:
            try:
                push_to_url(
                    bark_url,
                    title=supabase_realtime.default_sender(),
                    body=text,
                    group="bark-desktop",
                )
                delivered.append("Bark URL")
            except BarkError as exc:
                failures.append(f"Bark: {exc}")
        if delivered:
            self.log.info("self message sent via %s", ", ".join(delivered))
        for failure in failures:
            self.log.warning("self message failed: %s", failure)
        if failures:
            message = "; ".join(failures)
            title = "Message not sent" if not delivered else "Message partly sent"
            self._on_ui(lambda: self._notify_test_failure(message, title))

    # ------------------------------------------------------------------ #
    # IPC + shutdown
    # ------------------------------------------------------------------ #
    def _handle_ipc_command(self, payload: dict) -> dict:
        command = payload.get("command")
        notification_id = payload.get("notification_id")
        if command in ("open", "focus"):
            self._on_ui(lambda: self.open_history(notification_id))
        elif command == "setup":
            self._on_ui(self.open_setup)
        elif command == "quit":
            self._on_ui(lambda: self.request_quit(confirm=False))
        return {"ok": True}

    def request_quit(self, confirm: bool = True) -> None:
        from tkinter import messagebox

        parent = self.shell.root if self.shell else None
        if confirm:
            answer = messagebox.askyesno(
                "Quit Bark Desktop Client",
                "Quit the app? Notifications will stop until it is started again.",
                parent=parent,
            )
            if not answer:
                return
        if self.shell is not None:
            self.shell.stop()
        else:  # pragma: no cover - headless
            self.shutdown()

    def shutdown(self) -> None:
        """Stop everything.  Runs after the Tk main loop has exited.

        Every wait is bounded and short: the worker threads are daemons, so the
        process can exit while one of them is still unwinding.  `_stop_receiver`
        cancels the courier connection on the receiver's own event loop, which
        is what used to burn the full 5 s join timeout here.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        started = time.monotonic()
        self.log.info("shutting down")
        self._stop.set()
        self.stop_receiver(timeout=1.5)
        self.events.close()
        if self._ingest_thread is not None:
            self._ingest_thread.join(timeout=1.0)
            self._ingest_thread = None
        if self._maintenance_thread is not None:
            self._maintenance_thread.join(timeout=1.0)
            self._maintenance_thread = None
        self.fetcher.shutdown(timeout=1.0)
        self.webhook_dispatcher.close(timeout=1.0)
        self.bluetooth.stop(timeout=2.0)
        self.ancs.stop(timeout=4.0)
        if self._tts_preview is not None:
            self._tts_preview.shutdown(timeout=0.5)
            self._tts_preview = None
        try:
            self.tts.shutdown(timeout=1.0)
        except Exception:  # pragma: no cover - shutdown must continue
            pass
        if self.tray is not None:
            self.tray.stop()
            self.tray = None
        if self.settings_dialog is not None:
            self.settings_dialog.close()
            self.settings_dialog = None
        if self.routing_dialog is not None:
            self.routing_dialog.close()
            self.routing_dialog = None
        if self.setup_wizard is not None:
            self.setup_wizard.close()
            self.setup_wizard = None
        if self.history_window is not None:
            self.history_window.destroy()
            self.history_window = None
        try:
            self.db.close()
        except Exception:  # pragma: no cover
            pass
        self.single.release()
        self.log.info("shutdown complete in %.2f s", time.monotonic() - started)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _needs_setup(self) -> bool:
        meta = self.db.get_identity_meta()
        link = self.db.get_link()
        link_ok = bool(
            link
            and not link.is_supabase
            and link.is_usable
            and link.device_key
        )
        identity_ok = bool(meta and meta.state == models.DeviceState.ACTIVE)
        return not (identity_ok and link_ok)

    def _exe_path(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve()
        return Path(sys.executable).resolve()

    def _abandon_note(self) -> str:
        return ABANDON_NOTE
