"""Settings dialog: server, retention, images, toasts, silence, startup, wipe."""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import fields
from tkinter import messagebox, ttk

from ..bluetooth import (
    AncsState,
    AncsStatus,
    BluetoothDeviceInfo,
    SinkState,
    SinkStatus,
    UsbBluetoothAdapter,
    current_platform_note,
    describe_ancs_state,
    describe_state,
)
from ..core.config import (
    LOG_LEVELS,
    MESSAGE_ROUTE_BARK,
    MESSAGE_ROUTE_BOTH,
    MESSAGE_ROUTE_LABELS,
    MESSAGE_ROUTE_REALTIME,
    MESSAGE_ROUTES,
    Settings,
    supabase_display_url,
    validate_bark_push_url,
    validate_server_url,
    validate_supabase_url,
)
from ..core.models import now_ms
from ..tts import DEFAULT_TTS_PROVIDER, PROVIDER_LABEL_TO_NAME, PROVIDER_LABELS, TTS_PROVIDERS

logger = logging.getLogger("bark_pc.ui.settings")

SILENCE_CHOICES = {
    "Off": 0,
    "For 1 hour": 60 * 60 * 1000,
    "For 4 hours": 4 * 60 * 60 * 1000,
    "Until tomorrow": 24 * 60 * 60 * 1000,
}

# Live Bluetooth status lines share one row; the full text stays in the source
# variable, but the label only shows a single clipped line so it never wraps.
STATUS_MAX_CHARS = 48


def _one_line_status(text: str) -> str:
    """Collapse whitespace and cap a status message to one line."""
    line = " ".join(str(text or "").split())
    if len(line) <= STATUS_MAX_CHARS:
        return line
    return line[: STATUS_MAX_CHARS - 1].rstrip() + "…"


class SettingsDialog:
    def __init__(self, master, *, settings_store, controller, on_saved=None, on_closed=None):
        self.store = settings_store
        self.controller = controller
        self.on_saved = on_saved
        self.on_closed = on_closed
        self.settings = settings_store.settings
        self.window = tk.Toplevel(master)
        self.window.title("Bark Desktop Client — Settings")
        self.window.resizable(False, False)
        # Tk keeps a transient window withdrawn while its master is withdrawn.
        # The app shell root is a hidden tray window, so binding to it here
        # would make the dialog never appear.
        if master is not None and master.winfo_viewable():
            self.window.transient(master)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._load_values()
        self._fit_to_content()

    def _fit_to_content(self) -> None:
        """Size the window to its widgets so no control is ever clipped."""
        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        height = min(self.window.winfo_reqheight() + 80, self.window.winfo_screenheight() - 80)
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    # -- layout ------------------------------------------------------------ #
    def _build(self) -> None:
        container = ttk.Frame(self.window, padding=10)
        container.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(container)
        self.notebook.pack(fill="both", expand=True)
        connection = ttk.Frame(self.notebook, padding=(12, 10))
        bluetooth = ttk.Frame(self.notebook, padding=(12, 10))
        notifications = ttk.Frame(self.notebook, padding=(12, 10))
        maintenance = ttk.Frame(self.notebook, padding=(12, 10))
        self.notebook.add(connection, text="Connection")
        self.notebook.add(bluetooth, text="Bluetooth")
        self.notebook.add(notifications, text="Notifications")
        self.notebook.add(maintenance, text="Maintenance")

        self._build_connection_tab(connection)
        self._build_bluetooth_tab(bluetooth)
        self._build_notifications_tab(notifications)
        self._build_maintenance_tab(maintenance)

        self.status_label = ttk.Label(container, text="", style="Muted.TLabel")
        self.status_label.pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8)
        )

    def _build_connection_tab(self, tab: ttk.Frame) -> None:
        ttk.Label(tab, text="Bark server", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(tab, text="Server address").grid(row=1, column=0, sticky="w")
        self.server_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.server_var, width=34).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(tab, text="Push topic").grid(row=2, column=0, sticky="w")
        self.topic_var = tk.StringVar()
        ttk.Entry(tab, textvariable=self.topic_var, width=34).grid(
            row=2, column=1, sticky="ew", pady=2
        )

        ttk.Separator(tab).grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(tab, text="Supabase Realtime (optional)", style="Subtitle.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.supabase_enabled_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab,
            text="Receive broadcasts from a Supabase Realtime room",
            variable=self.supabase_enabled_var,
            command=self._toggle_supabase_fields,
        ).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(tab, text="Project URL").grid(
            row=6, column=0, sticky="w", pady=(4, 0)
        )
        self.supabase_url_var = tk.StringVar()
        self.supabase_url_entry = ttk.Entry(
            tab, textvariable=self.supabase_url_var, width=34
        )
        self.supabase_url_entry.grid(row=6, column=1, sticky="ew", pady=(4, 0))
        ttk.Label(tab, text="Auth key").grid(row=7, column=0, sticky="w")
        self.supabase_key_var = tk.StringVar()
        self.supabase_key_entry = ttk.Entry(
            tab, textvariable=self.supabase_key_var, width=34, show="*"
        )
        self.supabase_key_entry.grid(row=7, column=1, sticky="ew")
        ttk.Label(tab, text="Room name").grid(row=8, column=0, sticky="w")
        self.supabase_topic_var = tk.StringVar()
        self.supabase_topic_entry = ttk.Entry(
            tab, textvariable=self.supabase_topic_var, width=34
        )
        self.supabase_topic_entry.grid(row=8, column=1, sticky="ew")
        ttk.Label(
            tab,
            text="For example https://project.supabase.co",
            style="Muted.TLabel",
        ).grid(row=9, column=0, columnspan=2, sticky="w")

        ttk.Separator(tab).grid(row=10, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(tab, text="Compose-bar send", style="Subtitle.TLabel").grid(
            row=11, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(tab, text="Target").grid(
            row=12, column=0, sticky="w"
        )
        self.route_var = tk.StringVar()
        self.route_combo = ttk.Combobox(
            tab,
            textvariable=self.route_var,
            values=[MESSAGE_ROUTE_LABELS[name] for name in MESSAGE_ROUTES],
            state="readonly",
            width=24,
        )
        self.route_combo.grid(row=12, column=1, sticky="w", pady=2)
        self.route_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._toggle_route_fields()
        )
        ttk.Label(tab, text="Bark URL").grid(
            row=13, column=0, sticky="w"
        )
        self.route_url_var = tk.StringVar()
        self.route_url_entry = ttk.Entry(tab, textvariable=self.route_url_var, width=34)
        self.route_url_entry.grid(row=13, column=1, sticky="ew", pady=2)

        route_actions = ttk.Frame(tab)
        route_actions.grid(row=14, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ttk.Label(
            route_actions,
            text="For example https://api.day.app/<device_key>",
            style="Muted.TLabel",
        ).pack(side="left")

        ttk.Label(
            tab,
            text="Leave it empty to push to this device's own device key.",
            style="Muted.TLabel",
        ).grid(row=15, column=0, columnspan=2, sticky="w")

        # Only the input column grows: labels keep the left edge, entries run
        # all the way to the window's right edge.
        tab.columnconfigure(1, weight=1)

    def _build_bluetooth_tab(self, tab: ttk.Frame) -> None:
        self._bt_devices: list[tuple[str, BluetoothDeviceInfo]] = []
        self._bluetooth_loaded = False
        self._bluetooth_after_id: str | None = None
        self._bluetooth_notice = ""
        self._ancs_adapters: list[tuple[str, UsbBluetoothAdapter]] = []
        self._ancs_loaded = False
        self._ancs_notice = ""

        ttk.Label(tab, text="Bluetooth audio", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.bluetooth_var = tk.BooleanVar()
        self.bluetooth_check = ttk.Checkbutton(
            tab,
            text="Use this PC as a Bluetooth speaker (A2DP sink)",
            variable=self.bluetooth_var,
            command=self._toggle_bluetooth_fields,
        )
        self.bluetooth_check.grid(row=1, column=0, columnspan=2, sticky="w")

        ttk.Label(tab, text="Paired device").grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.bluetooth_device_var = tk.StringVar()
        self.bluetooth_combo = ttk.Combobox(
            tab,
            textvariable=self.bluetooth_device_var,
            state="disabled",
            width=38,
        )
        self.bluetooth_combo.grid(row=2, column=1, sticky="ew", pady=(4, 0))
        self.bluetooth_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_bluetooth_buttons()
        )

        actions = ttk.Frame(tab)
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.bluetooth_refresh_button = ttk.Button(
            actions, text="Refresh", command=self._refresh_bluetooth_devices
        )
        self.bluetooth_refresh_button.grid(row=0, column=0, sticky="w")
        self.bluetooth_connect_button = ttk.Button(
            actions, text="Connect", command=self._bluetooth_connect
        )
        self.bluetooth_connect_button.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self.bluetooth_disconnect_button = ttk.Button(
            actions, text="Disconnect", command=self._bluetooth_disconnect
        )
        self.bluetooth_disconnect_button.grid(row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Button(
            actions,
            text="Bluetooth settings",
            command=self._open_bluetooth_settings,
        ).grid(row=0, column=3, sticky="w", padx=(6, 0))

        ttk.Label(tab, text="Status").grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.bluetooth_status_var = tk.StringVar(value="Off")
        self.bluetooth_status_display_var = self._status_display(
            self.bluetooth_status_var
        )
        ttk.Label(
            tab,
            textvariable=self.bluetooth_status_display_var,
            style="Muted.TLabel",
        ).grid(row=4, column=1, sticky="w", pady=(8, 0))

        ttk.Label(
            tab,
            text="Pair the phone in system first, then press Refresh.",
            style="Muted.TLabel",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        ttk.Separator(tab).grid(row=6, column=0, columnspan=2, sticky="ew", pady=10)

        ttk.Label(tab, text="iOS notifications (ANCS)", style="Subtitle.TLabel").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.ancs_var = tk.BooleanVar()
        self.ancs_check = ttk.Checkbutton(
            tab,
            text="Receive iOS notifications",
            variable=self.ancs_var,
            command=self._toggle_ancs_fields,
        )
        self.ancs_check.grid(row=8, column=0, columnspan=2, sticky="w")

        ttk.Label(tab, text="USB Bluetooth adapter").grid(
            row=9, column=0, sticky="w", pady=(4, 0)
        )
        self.ancs_adapter_var = tk.StringVar()
        self.ancs_combo = ttk.Combobox(
            tab,
            textvariable=self.ancs_adapter_var,
            state="disabled",
            width=38,
        )
        self.ancs_combo.grid(row=9, column=1, sticky="ew", pady=(4, 0))
        self.ancs_combo.bind(
            "<<ComboboxSelected>>", lambda _event: self._sync_ancs_buttons()
        )

        ancs_actions = ttk.Frame(tab)
        ancs_actions.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.ancs_refresh_button = ttk.Button(
            ancs_actions, text="Refresh", command=self._refresh_ancs_adapters
        )
        self.ancs_refresh_button.grid(row=0, column=0, sticky="w")
        self.ancs_restart_button = ttk.Button(
            ancs_actions, text="Restart receiver", command=self._restart_ancs
        )
        self.ancs_restart_button.grid(row=0, column=1, sticky="w", padx=(6, 0))

        ttk.Label(tab, text="Status").grid(row=11, column=0, sticky="w", pady=(8, 0))
        self.ancs_status_var = tk.StringVar(value="Off")
        self.ancs_status_display_var = self._status_display(self.ancs_status_var)
        ttk.Label(
            tab,
            textvariable=self.ancs_status_display_var,
            style="Muted.TLabel",
        ).grid(row=11, column=1, sticky="w", pady=(8, 0))

        self.ancs_hint_var = tk.StringVar(value=current_platform_note())
        ttk.Label(
            tab,
            textvariable=self.ancs_hint_var,
            style="Muted.TLabel",
            justify="left",
        ).grid(row=12, column=0, columnspan=2, sticky="w", pady=(4, 0))
        tab.columnconfigure(1, weight=1)

    def _status_display(self, source: tk.StringVar) -> tk.StringVar:
        """A single-line view of *source*; the full text stays in the source var."""
        display = tk.StringVar(value=_one_line_status(source.get()))
        source.trace_add(
            "write", lambda *_args: display.set(_one_line_status(source.get()))
        )
        return display

    def _build_notifications_tab(self, tab: ttk.Frame) -> None:
        ttk.Label(tab, text="History and images", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(tab, text="Keep at most (50–10 000)").grid(row=1, column=0, sticky="w")
        self.count_var = tk.StringVar()
        ttk.Spinbox(
            tab, from_=50, to=10_000, increment=50, textvariable=self.count_var, width=10
        ).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(tab, text="Keep for at most (1–365 days)").grid(
            row=2, column=0, sticky="w"
        )
        self.days_var = tk.StringVar()
        ttk.Spinbox(tab, from_=1, to=365, textvariable=self.days_var, width=10).grid(
            row=2, column=1, sticky="w", pady=2
        )
        ttk.Label(tab, text="Image cache (20–2 000 MB)").grid(row=3, column=0, sticky="w")
        self.cache_var = tk.StringVar()
        ttk.Spinbox(
            tab, from_=20, to=2_000, increment=20, textvariable=self.cache_var, width=10
        ).grid(row=3, column=1, sticky="w", pady=2)

        ttk.Separator(tab).grid(row=4, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(tab, text="Notifications", style="Subtitle.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.toasts_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab, text="Show system notifications", variable=self.toasts_var
        ).grid(row=6, column=0, columnspan=2, sticky="w")
        self.hide_preview_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab,
            text='Hide preview text (show "New notification" instead)',
            variable=self.hide_preview_var,
        ).grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Label(tab, text="Silence notifications").grid(
            row=8, column=0, sticky="w", pady=(2, 0)
        )
        self.silence_var = tk.StringVar(value="Off")
        ttk.Combobox(
            tab,
            textvariable=self.silence_var,
            values=list(SILENCE_CHOICES),
            state="readonly",
            width=20,
        ).grid(row=8, column=1, sticky="w", pady=(2, 0))

        ttk.Separator(tab).grid(row=9, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(tab, text="Text-to-speech", style="Subtitle.TLabel").grid(
            row=10, column=0, columnspan=2, sticky="w"
        )
        self.tts_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab, text="Speak notifications aloud", variable=self.tts_var
        ).grid(row=11, column=0, sticky="w")
        self.tts_provider_var = tk.StringVar()
        ttk.Combobox(
            tab,
            textvariable=self.tts_provider_var,
            values=[PROVIDER_LABELS[name] for name in TTS_PROVIDERS],
            state="readonly",
            width=22,
        ).grid(row=11, column=1, sticky="w")
        self.tts_body_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab, text="Read the message body too", variable=self.tts_body_var
        ).grid(row=12, column=0, sticky="w")
        voice = ttk.Frame(tab)
        voice.grid(row=12, column=1, sticky="w")
        self.tts_rate_var = tk.StringVar()
        ttk.Spinbox(voice, from_=-10, to=10, textvariable=self.tts_rate_var, width=4).pack(
            side="left"
        )
        self.tts_volume_var = tk.StringVar()
        ttk.Spinbox(
            voice, from_=0, to=100, increment=10, textvariable=self.tts_volume_var, width=4
        ).pack(side="left", padx=(6, 0))
        ttk.Label(voice, text="rate / volume", style="Muted.TLabel").pack(
            side="left", padx=(6, 0)
        )
        self.tts_unlocked_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab,
            text="Only read aloud while the screen is unlocked",
            variable=self.tts_unlocked_var,
        ).grid(row=13, column=0, columnspan=2, sticky="w")
        ttk.Button(tab, text="Test voice", command=self._test_voice).grid(
            row=14, column=0, sticky="w", pady=(6, 0)
        )

        tab.columnconfigure(1, weight=1)

    def _build_maintenance_tab(self, tab: ttk.Frame) -> None:
        ttk.Label(tab, text="Maintenance", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(
            tab,
            text="Check the configured server, collect logs for support",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        actions = ttk.Frame(tab)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        ttk.Button(actions, text="Check connection", command=self._check_connection).grid(
            row=0, column=0, sticky="ew", pady=2
        )
        ttk.Button(actions, text="Export diagnostics", command=self._export).grid(
            row=0, column=1, sticky="ew", padx=(6, 0), pady=2
        )
        ttk.Button(actions, text="Clear history", command=self._clear_history).grid(
            row=1, column=0, sticky="ew", pady=2
        )
        ttk.Button(actions, text="Remove all data", command=self._remove_all).grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=2
        )

        ttk.Separator(tab).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 6))

        ttk.Label(tab, text="Startup", style="Subtitle.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(
            tab,
            text="Start on user login (no admin rights)",
            variable=self.autostart_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(
            tab,
            text="Adds a per-user entry to the Windows Run key for the current program path.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Separator(tab).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 6))

        ttk.Label(tab, text="Logging", style="Subtitle.TLabel").grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(tab, text="Log level").grid(row=9, column=0, sticky="w")
        self.log_level_var = tk.StringVar(value="info")
        ttk.Combobox(
            tab,
            textvariable=self.log_level_var,
            values=list(LOG_LEVELS),
            state="readonly",
            width=12,
        ).grid(row=9, column=1, sticky="w")
        ttk.Label(
            tab,
            text="Debug also records every outbound HTTP request and response.",
            style="Muted.TLabel",
            justify="left",
        ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(2, 0))
        tab.columnconfigure(1, weight=1)

    # -- values ------------------------------------------------------------ #
    def _load_values(self) -> None:
        settings = self.settings
        self.server_var.set(settings.server_url)
        self.topic_var.set(settings.push_topic)
        self.supabase_enabled_var.set(bool(settings.supabase_enabled))
        self.supabase_url_var.set(supabase_display_url(settings.supabase_url))
        self.supabase_key_var.set(settings.supabase_key)
        self.supabase_topic_var.set(settings.supabase_topic or "bark")
        self._toggle_supabase_fields()
        self.route_var.set(
            MESSAGE_ROUTE_LABELS.get(
                settings.message_route, MESSAGE_ROUTE_LABELS[MESSAGE_ROUTE_REALTIME]
            )
        )
        self.route_url_var.set(settings.message_route_url)
        self._toggle_route_fields()
        self.count_var.set(str(settings.history_retention_count))
        self.days_var.set(str(settings.history_retention_days))
        self.cache_var.set(str(settings.image_cache_mb))
        self.toasts_var.set(bool(settings.toasts_enabled))
        self.hide_preview_var.set(bool(settings.hide_preview_text))
        self.bluetooth_var.set(bool(settings.bluetooth_enabled))
        self.bluetooth_device_var.set(settings.bluetooth_device_name or "")
        self._toggle_bluetooth_fields()
        self.ancs_var.set(bool(settings.ancs_enabled))
        self.ancs_adapter_var.set(settings.ancs_adapter_name or "")
        self._sync_ancs_buttons()
        self.autostart_var.set(bool(settings.autostart_enabled))
        self.log_level_var.set(settings.log_level)
        self.tts_var.set(bool(settings.tts_enabled))
        self.tts_provider_var.set(
            PROVIDER_LABELS.get(settings.tts_provider, PROVIDER_LABELS[DEFAULT_TTS_PROVIDER])
        )
        self.tts_rate_var.set(str(settings.tts_rate))
        self.tts_volume_var.set(str(settings.tts_volume))
        self.tts_body_var.set(bool(settings.tts_include_body))
        self.tts_unlocked_var.set(bool(settings.tts_only_when_unlocked))
        self.silence_var.set("Off")
        if settings.silence_until and settings.silence_until > now_ms():
            self.silence_var.set("For 1 hour")

    def _toggle_supabase_fields(self) -> None:
        state = "normal" if self.supabase_enabled_var.get() else "disabled"
        for entry in (
            self.supabase_url_entry,
            self.supabase_key_entry,
            self.supabase_topic_entry,
        ):
            entry.configure(state=state)

    def _toggle_route_fields(self) -> None:
        route = self._selected_route()
        state = "normal" if route in (MESSAGE_ROUTE_BARK, MESSAGE_ROUTE_BOTH) else "disabled"
        self.route_url_entry.configure(state=state)

    def _selected_route(self) -> str:
        label = self.route_var.get()
        for name in MESSAGE_ROUTES:
            if MESSAGE_ROUTE_LABELS[name] == label:
                return name
        return MESSAGE_ROUTE_REALTIME

    def _candidate(self) -> dict:
        silence_choice = self.silence_var.get()
        silence_until = None
        if silence_choice in SILENCE_CHOICES and SILENCE_CHOICES[silence_choice]:
            silence_until = now_ms() + SILENCE_CHOICES[silence_choice]
        bluetooth_device = self._selected_bluetooth()
        if bluetooth_device is not None:
            bluetooth_device_id = bluetooth_device.id
            bluetooth_device_name = bluetooth_device.name
        elif not self._bluetooth_loaded:
            bluetooth_device_id = self.settings.bluetooth_device_id
            bluetooth_device_name = self.settings.bluetooth_device_name
        else:
            bluetooth_device_id = ""
            bluetooth_device_name = ""
        ancs_adapter = self._selected_ancs_adapter()
        if ancs_adapter is not None:
            ancs_adapter_id = ancs_adapter.selector
            ancs_adapter_name = ancs_adapter.name
        elif not self._ancs_loaded:
            ancs_adapter_id = self.settings.ancs_adapter
            ancs_adapter_name = self.settings.ancs_adapter_name
        else:
            ancs_adapter_id = ""
            ancs_adapter_name = ""
        server_url = validate_server_url(self.server_var.get())
        if server_url.lower().startswith("sb://"):
            raise ValueError(
                "The Bark server must use http:// or https://; Supabase Realtime is "
                "configured separately below."
            )
        return {
            "server_url": server_url,
            "push_topic": self.topic_var.get().strip(),
            "supabase_enabled": bool(self.supabase_enabled_var.get()),
            "supabase_url": validate_supabase_url(self.supabase_url_var.get()),
            "supabase_key": self.supabase_key_var.get().strip(),
            "supabase_topic": self.supabase_topic_var.get().strip() or "bark",
            "message_route": self._selected_route(),
            "message_route_url": validate_bark_push_url(self.route_url_var.get()),
            "history_retention_count": int(self.count_var.get()),
            "history_retention_days": int(self.days_var.get()),
            "image_cache_mb": int(self.cache_var.get()),
            "toasts_enabled": bool(self.toasts_var.get()),
            "hide_preview_text": bool(self.hide_preview_var.get()),
            "bluetooth_enabled": bool(self.bluetooth_var.get()),
            "bluetooth_device_id": bluetooth_device_id,
            "bluetooth_device_name": bluetooth_device_name,
            "ancs_enabled": bool(self.ancs_var.get()),
            "ancs_adapter": ancs_adapter_id,
            "ancs_adapter_name": ancs_adapter_name,
            "tts_enabled": bool(self.tts_var.get()),
            "tts_provider": PROVIDER_LABEL_TO_NAME.get(
                self.tts_provider_var.get(), DEFAULT_TTS_PROVIDER
            ),
            "tts_include_body": bool(self.tts_body_var.get()),
            "tts_only_when_unlocked": bool(self.tts_unlocked_var.get()),
            "tts_rate": int(self.tts_rate_var.get()),
            "tts_volume": int(self.tts_volume_var.get()),
            "silence_until": silence_until,
            "autostart_enabled": bool(self.autostart_var.get()),
            "log_level": self.log_level_var.get().strip() or self.settings.log_level,
            "last_connected_at": self.settings.last_connected_at,
        }

    # -- Bluetooth tab ----------------------------------------------------- #
    def _bluetooth_support(self) -> tuple[bool, str]:
        probe = getattr(self.controller, "bluetooth_support", None)
        if probe is None:
            return False, "Bluetooth audio receiving is unavailable in this build."
        try:
            return probe()
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    def _selected_bluetooth(self) -> BluetoothDeviceInfo | None:
        label = self.bluetooth_device_var.get()
        for choice_label, device in self._bt_devices:
            if choice_label == label:
                return device
        return None

    def _selected_bluetooth_id(self) -> str:
        device = self._selected_bluetooth()
        return device.id if device is not None else ""

    def _refresh_bluetooth_devices(self, select_id: str | None = None) -> None:
        self._bluetooth_loaded = True
        if select_id is None:
            selected = self._selected_bluetooth()
            select_id = (
                selected.id if selected is not None else self.settings.bluetooth_device_id
            )
        supported, reason = self._bluetooth_support()
        if not supported:
            self._bt_devices = []
            self._bluetooth_notice = reason
            self.bluetooth_check.configure(state="disabled")
            self.bluetooth_combo.configure(values=[], state="disabled")
            self.bluetooth_status_var.set(reason)
            self._sync_bluetooth_buttons()
            return
        self.bluetooth_check.configure(state="normal")

        devices: list[BluetoothDeviceInfo] = []
        error = ""
        lister = getattr(self.controller, "bluetooth_devices", None)
        if lister is not None:
            try:
                devices, error = lister()
            except Exception as exc:  # pragma: no cover - defensive
                error = f"Could not list Bluetooth devices: {exc}"
        saved_id = self.settings.bluetooth_device_id
        if saved_id and all(device.id != saved_id for device in devices):
            devices.append(
                BluetoothDeviceInfo(
                    id=saved_id,
                    name=self.settings.bluetooth_device_name or "Saved Bluetooth device",
                )
            )

        self._bt_devices = _device_choices(devices)
        labels = [label for label, _ in self._bt_devices]
        self.bluetooth_combo.configure(values=labels, state="readonly")
        if select_id:
            for index, (_label, device) in enumerate(self._bt_devices):
                if device.id == select_id:
                    self.bluetooth_combo.current(index)
                    break
            else:
                if labels:
                    self.bluetooth_combo.current(0)
        elif labels:
            self.bluetooth_combo.current(0)

        if error:
            self._bluetooth_notice = error
            self.bluetooth_status_var.set(error)
        elif not devices:
            self._bluetooth_notice = (
                "No paired device found. Pair it in system, then press Refresh."
            )
            self.bluetooth_status_var.set(self._bluetooth_notice)
        else:
            self._bluetooth_notice = ""
            self.bluetooth_status_var.set("Select a device, then press Connect.")
        self._sync_bluetooth_buttons()

    def _toggle_bluetooth_fields(self) -> None:
        self._sync_bluetooth_buttons()

    def _sync_bluetooth_buttons(self, state: SinkState | None = None) -> None:
        enabled = bool(self.bluetooth_var.get())
        has_device = bool(self._bt_devices)
        active = True if state is None else state.is_running
        self.bluetooth_combo.configure(
            state="readonly" if enabled and has_device else "disabled"
        )
        self.bluetooth_connect_button.configure(
            state="normal" if enabled and has_device else "disabled"
        )
        self.bluetooth_disconnect_button.configure(
            state="normal" if enabled and active else "disabled"
        )

    def _schedule_bluetooth_poll(self) -> None:
        self._cancel_bluetooth_poll()
        try:
            self._bluetooth_after_id = self.window.after(300, self._poll_bluetooth)
        except tk.TclError:  # pragma: no cover - window already gone
            self._bluetooth_after_id = None

    def _cancel_bluetooth_poll(self) -> None:
        if self._bluetooth_after_id is None:
            return
        try:
            self.window.after_cancel(self._bluetooth_after_id)
        except Exception:  # pragma: no cover - already fired
            pass
        self._bluetooth_after_id = None

    def _poll_bluetooth(self) -> None:
        self._bluetooth_after_id = None
        try:
            if not self.window.winfo_exists():
                return
        except tk.TclError:  # pragma: no cover - window destroyed
            return
        if not self._bluetooth_loaded:
            self._refresh_bluetooth_devices()
        state = SinkState()
        reader = getattr(self.controller, "bluetooth_state", None)
        if reader is not None:
            try:
                state = reader()
            except Exception:  # pragma: no cover - defensive
                state = SinkState()
        text = describe_state(state)
        if state.status is SinkStatus.OFF and self._bluetooth_notice:
            text = self._bluetooth_notice
        self.bluetooth_status_var.set(text)
        self._sync_bluetooth_buttons(state)

        if not self._ancs_loaded and self.ancs_var.get():
            self._refresh_ancs_adapters()
        ancs_state = AncsState()
        ancs_reader = getattr(self.controller, "ancs_state", None)
        if ancs_reader is not None:
            try:
                ancs_state = ancs_reader()
            except Exception:  # pragma: no cover - defensive
                ancs_state = AncsState()
        ancs_text = describe_ancs_state(ancs_state)
        if ancs_state.status is AncsStatus.OFF and self._ancs_notice:
            ancs_text = self._ancs_notice
        self.ancs_status_var.set(ancs_text)
        self._sync_ancs_buttons(ancs_state)
        try:
            self._bluetooth_after_id = self.window.after(700, self._poll_bluetooth)
        except tk.TclError:  # pragma: no cover
            self._bluetooth_after_id = None

    def _bluetooth_connect(self) -> None:
        device = self._selected_bluetooth()
        if device is None:
            messagebox.showinfo(
                "Bluetooth speaker",
                "Choose a paired Bluetooth device first.",
                parent=self.window,
            )
            return
        connect = getattr(self.controller, "bluetooth_connect", None)
        if connect is None:
            return
        try:
            connect(device.id, device.name)
        except Exception as exc:  # pragma: no cover - defensive
            messagebox.showerror("Bluetooth speaker", str(exc), parent=self.window)
            return
        self.bluetooth_status_var.set(f"Connecting to {device.label}…")

    def _bluetooth_disconnect(self) -> None:
        disconnect = getattr(self.controller, "bluetooth_disconnect", None)
        if disconnect is None:
            return
        try:
            disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            messagebox.showerror("Bluetooth speaker", str(exc), parent=self.window)
            return
        self.bluetooth_status_var.set("Disconnecting…")

    def _open_bluetooth_settings(self) -> None:
        opener = getattr(self.controller, "open_bluetooth_settings", None)
        if opener is not None:
            opener()

    # -- ANCS (iOS notifications) -------------------------------------- #
    def _ancs_support(self) -> tuple[bool, str]:
        probe = getattr(self.controller, "ancs_support", None)
        if probe is None:
            return False, "iOS notifications over Bluetooth are unavailable in this build."
        try:
            return probe()
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    def _selected_ancs_adapter(self) -> UsbBluetoothAdapter | None:
        label = self.ancs_adapter_var.get()
        for choice_label, adapter in self._ancs_adapters:
            if choice_label == label:
                return adapter
        return None

    def _refresh_ancs_adapters(self, select_selector: str | None = None) -> None:
        self._ancs_loaded = True
        if select_selector is None:
            selected = self._selected_ancs_adapter()
            select_selector = (
                selected.selector if selected is not None else self.settings.ancs_adapter
            )
        supported, reason = self._ancs_support()
        if not supported:
            self._ancs_adapters = []
            self._ancs_notice = reason
            self.ancs_check.configure(state="disabled")
            self.ancs_combo.configure(values=[], state="disabled")
            self.ancs_status_var.set(reason)
            self._sync_ancs_buttons()
            return
        self.ancs_check.configure(state="normal")

        adapters: list[UsbBluetoothAdapter] = []
        error = ""
        lister = getattr(self.controller, "ancs_adapters", None)
        if lister is not None:
            try:
                adapters, error = lister()
            except Exception as exc:  # pragma: no cover - defensive
                error = f"Could not list USB Bluetooth adapters: {exc}"
        saved_selector = self.settings.ancs_adapter
        if saved_selector and all(
            adapter.selector != saved_selector for adapter in adapters
        ):
            adapters.append(
                UsbBluetoothAdapter(
                    selector=saved_selector,
                    name=self.settings.ancs_adapter_name or "Saved USB Bluetooth adapter",
                    vendor_id=0,
                    product_id=0,
                )
            )

        self._ancs_adapters = _adapter_choices(adapters)
        labels = [label for label, _adapter in self._ancs_adapters]
        self.ancs_combo.configure(values=labels, state="readonly")
        if select_selector:
            for index, (_label, adapter) in enumerate(self._ancs_adapters):
                if adapter.selector == select_selector:
                    self.ancs_combo.current(index)
                    break
            else:
                if labels:
                    self.ancs_combo.current(0)
        elif labels:
            self.ancs_combo.current(0)

        if error:
            self._ancs_notice = error
            self.ancs_status_var.set(error)
        elif not adapters:
            self._ancs_notice = (
                "No USB Bluetooth adapter found. Plug one in and press Refresh."
            )
            self.ancs_status_var.set(self._ancs_notice)
        else:
            self._ancs_notice = ""
            self.ancs_status_var.set("Select an adapter, then save.")
        self._sync_ancs_buttons()

    def _toggle_ancs_fields(self) -> None:
        if self.ancs_var.get() and not self._ancs_loaded:
            self._refresh_ancs_adapters()
        self._sync_ancs_buttons()

    def _sync_ancs_buttons(self, state: AncsState | None = None) -> None:
        enabled = bool(self.ancs_var.get())
        has_adapter = bool(self._ancs_adapters)
        self.ancs_combo.configure(
            state="readonly" if enabled and has_adapter else "disabled"
        )
        self.ancs_restart_button.configure(state="normal" if enabled else "disabled")

    def _restart_ancs(self) -> None:
        restart = getattr(self.controller, "ancs_restart", None)
        if restart is None:
            return
        try:
            restart()
        except Exception as exc:  # pragma: no cover - defensive
            messagebox.showerror("iOS notifications", str(exc), parent=self.window)
            return
        self.ancs_status_var.set("Restarting the receiver…")

    # -- actions ----------------------------------------------------------- #
    def _save(self) -> None:
        try:
            candidate = self._candidate()
        except ValueError as exc:
            messagebox.showerror("Invalid setting", str(exc), parent=self.window)
            return
        if candidate["supabase_enabled"] and not candidate["supabase_url"]:
            messagebox.showerror(
                "Invalid setting",
                "Enter the Supabase project URL (https://project.supabase.co).",
                parent=self.window,
            )
            return
        if candidate["supabase_enabled"] and not candidate["supabase_key"]:
            messagebox.showerror(
                "Invalid setting",
                "Supabase Realtime needs the auth key (anon/publishable key).",
                parent=self.window,
            )
            return
        if candidate["bluetooth_enabled"] and not candidate["bluetooth_device_id"]:
            messagebox.showerror(
                "Bluetooth speaker",
                "Choose a paired Bluetooth device. If the list is empty, pair the "
                "phone in Bluetooth Settings and press Refresh.",
                parent=self.window,
            )
            return
        if candidate["ancs_enabled"] and not candidate["ancs_adapter"]:
            messagebox.showerror(
                "iOS notifications",
                "Choose the USB Bluetooth adapter that Bumble should use. If the "
                "list is empty, plug the adapter in and press Refresh.",
                parent=self.window,
            )
            return
        validated = Settings.from_dict(
            {**self.settings.to_dict(), **candidate, "warnings": []}, None
        )
        self.store.save(validated)
        self.settings = validated
        if self.on_saved is not None:
            try:
                self.on_saved(validated)
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("settings saved callback failed")
        self.status_label.configure(text="Settings saved.")
        self.close()

    def _test_voice(self) -> None:
        """Speak a sample line with the values currently in the dialog."""
        try:
            provider = PROVIDER_LABEL_TO_NAME.get(
                self.tts_provider_var.get(), DEFAULT_TTS_PROVIDER
            )
            rate = int(self.tts_rate_var.get())
            volume = int(self.tts_volume_var.get())
        except (TypeError, ValueError):
            self.status_label.configure(text="Rate and volume must be whole numbers.")
            return
        if provider == "none":
            self.status_label.configure(text="No voice provider is selected.")
            return
        try:
            speaking = self.controller.preview_tts(provider=provider, rate=rate, volume=volume)
        except Exception as exc:  # pragma: no cover - controller isolation
            logger.exception("voice preview failed")
            self.status_label.configure(text=f"Voice preview failed: {exc}")
            return
        self.status_label.configure(
            text="Speaking the sample line…"
            if speaking
            else "No speech voice is available on this system."
        )

    def _check_connection(self) -> None:
        self.status_label.configure(text="Checking…")

        def done(ok: bool, message: str) -> None:
            self.window.after(0, lambda: self.status_label.configure(text=message))

        self.controller.check_connection_async(done)

    def _export(self) -> None:
        try:
            path = self.controller.export_diagnostics()
        except Exception as exc:
            self.status_label.configure(text=f"Export failed: {exc}")
            return
        if path:
            self.status_label.configure(text=f"Diagnostics written to {path}")

    def _clear_history(self) -> None:
        if not messagebox.askyesno(
            "Clear history", "Delete all locally stored notifications?", parent=self.window
        ):
            return
        removed = self.controller.clear_history()
        self.status_label.configure(text=f"Removed {removed} notification(s).")

    def _remove_all(self) -> None:
        if not messagebox.askyesno(
            "Remove all data",
            "This removes the device identity, history, images, logs, settings, "
            "webhook routing rules, the autostart entry and the barkpc: registration.\n\n"
            "Continue?",
            parent=self.window,
        ):
            return
        try:
            report = self.controller.remove_all_data()
        except Exception as exc:
            messagebox.showerror("Remove all data", str(exc), parent=self.window)
            return
        messagebox.showinfo("Remove all data", report, parent=self.window)
        self.close()

    def show(self) -> None:
        self.window.deiconify()
        self._schedule_bluetooth_poll()
        self.window.lift()
        self.window.focus_force()
        # Without a visible owner the dialog can still open behind other
        # windows; a quick topmost flip brings it to the foreground.
        try:
            self.window.attributes("-topmost", True)
            self.window.attributes("-topmost", False)
        except Exception:  # pragma: no cover - attribute support varies
            pass

    def close(self) -> None:
        self._cancel_bluetooth_poll()
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass
        if self.on_closed is not None:
            self.on_closed()


def _device_choices(
    devices: list[BluetoothDeviceInfo],
) -> list[tuple[str, BluetoothDeviceInfo]]:
    """Give duplicate device names distinct labels while keeping the id mapping."""
    seen: dict[str, int] = {}
    choices: list[tuple[str, BluetoothDeviceInfo]] = []
    for device in devices:
        base = device.label
        seen[base] = seen.get(base, 0) + 1
        label = base if seen[base] == 1 else f"{base} ({seen[base]})"
        choices.append((label, device))
    return choices


def _adapter_choices(
    adapters: list[UsbBluetoothAdapter],
) -> list[tuple[str, UsbBluetoothAdapter]]:
    """Same idea for USB adapters: two identical dongles stay distinguishable."""
    seen: dict[str, int] = {}
    choices: list[tuple[str, UsbBluetoothAdapter]] = []
    for adapter in adapters:
        base = adapter.label
        seen[base] = seen.get(base, 0) + 1
        label = base if seen[base] == 1 else f"{base} ({seen[base]})"
        choices.append((label, adapter))
    return choices


def settings_field_names() -> list[str]:
    return [item.name for item in fields(Settings)]
