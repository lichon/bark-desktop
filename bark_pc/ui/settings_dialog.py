"""Settings dialog: server, retention, images, toasts, silence, autostart, wipe."""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import fields
from tkinter import messagebox, ttk

from ..core.config import Settings, validate_server_url
from ..core.models import now_ms
from ..tts import DEFAULT_TTS_PROVIDER, PROVIDER_LABEL_TO_NAME, PROVIDER_LABELS, TTS_PROVIDERS

logger = logging.getLogger("bark_pc.ui.settings")

SILENCE_CHOICES = {
    "Off": 0,
    "For 1 hour": 60 * 60 * 1000,
    "For 4 hours": 4 * 60 * 60 * 1000,
    "Until tomorrow": 24 * 60 * 60 * 1000,
}


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
        width = max(560, self.window.winfo_reqwidth())
        height = min(self.window.winfo_reqheight(), self.window.winfo_screenheight() - 80)
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    # -- layout ------------------------------------------------------------ #
    def _build(self) -> None:
        container = ttk.Frame(self.window, padding=14)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="Server", style="Subtitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(container, text="Server address").grid(row=1, column=0, sticky="w")
        self.server_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.server_var, width=42).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(container, text="Push topic (advanced)").grid(row=2, column=0, sticky="w")
        self.topic_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.topic_var, width=42).grid(
            row=2, column=1, sticky="ew", pady=2
        )

        ttk.Separator(container).grid(row=3, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(container, text="History and images", style="Subtitle.TLabel").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        ttk.Label(container, text="Keep at most (50–10 000)").grid(row=5, column=0, sticky="w")
        self.count_var = tk.StringVar()
        ttk.Spinbox(
            container, from_=50, to=10_000, increment=50, textvariable=self.count_var, width=10
        ).grid(row=5, column=1, sticky="w", pady=2)
        ttk.Label(container, text="Keep for at most (1–365 days)").grid(row=6, column=0, sticky="w")
        self.days_var = tk.StringVar()
        ttk.Spinbox(container, from_=1, to=365, textvariable=self.days_var, width=10).grid(
            row=6, column=1, sticky="w", pady=2
        )
        ttk.Label(container, text="Image cache (20–2 000 MB)").grid(row=7, column=0, sticky="w")
        self.cache_var = tk.StringVar()
        ttk.Spinbox(
            container, from_=20, to=2_000, increment=20, textvariable=self.cache_var, width=10
        ).grid(row=7, column=1, sticky="w", pady=2)

        ttk.Separator(container).grid(row=8, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(container, text="Notifications", style="Subtitle.TLabel").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, 4)
        )
        self.toasts_var = tk.BooleanVar()
        ttk.Checkbutton(
            container, text="Show Windows notifications", variable=self.toasts_var
        ).grid(row=10, column=0, columnspan=2, sticky="w")
        self.hide_preview_var = tk.BooleanVar()
        ttk.Checkbutton(
            container,
            text='Hide preview text (show "New notification" instead)',
            variable=self.hide_preview_var,
        ).grid(row=11, column=0, columnspan=2, sticky="w")
        ttk.Label(container, text="Silence notifications").grid(
            row=12, column=0, sticky="w", pady=(2, 0)
        )
        self.silence_var = tk.StringVar(value="Off")
        ttk.Combobox(
            container,
            textvariable=self.silence_var,
            values=list(SILENCE_CHOICES),
            state="readonly",
            width=20,
        ).grid(row=12, column=1, sticky="w", pady=(2, 0))
        self.autostart_var = tk.BooleanVar()
        ttk.Checkbutton(
            container, text="Start with Windows (no admin rights)", variable=self.autostart_var
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(2, 0))

        ttk.Separator(container).grid(row=14, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(container, text="Text-to-speech", style="Subtitle.TLabel").grid(
            row=15, column=0, columnspan=2, sticky="w"
        )
        self.tts_var = tk.BooleanVar()
        ttk.Checkbutton(
            container, text="Speak notifications aloud", variable=self.tts_var
        ).grid(row=16, column=0, sticky="w")
        self.tts_provider_var = tk.StringVar()
        ttk.Combobox(
            container,
            textvariable=self.tts_provider_var,
            values=[PROVIDER_LABELS[name] for name in TTS_PROVIDERS],
            state="readonly",
            width=22,
        ).grid(row=16, column=1, sticky="w")
        self.tts_body_var = tk.BooleanVar()
        ttk.Checkbutton(
            container, text="Read the message body too", variable=self.tts_body_var
        ).grid(row=17, column=0, sticky="w")
        voice = ttk.Frame(container)
        voice.grid(row=17, column=1, sticky="w")
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
        ttk.Button(voice, text="Test voice", command=self._test_voice).pack(
            side="left", padx=(10, 0)
        )

        ttk.Separator(container).grid(row=18, column=0, columnspan=2, sticky="ew", pady=6)

        actions = ttk.Frame(container)
        actions.grid(row=19, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="Check connection", command=self._check_connection).pack(
            side="left"
        )
        ttk.Button(actions, text="Export diagnostics", command=self._export).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Clear history", command=self._clear_history).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Remove all data", command=self._remove_all).pack(
            side="left", padx=(6, 0)
        )

        self.status_label = ttk.Label(container, text="", style="Muted.TLabel", wraplength=500)
        self.status_label.grid(row=20, column=0, columnspan=2, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(container)
        buttons.grid(row=21, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8)
        )
        container.columnconfigure(1, weight=1)

    # -- values ------------------------------------------------------------ #
    def _load_values(self) -> None:
        settings = self.settings
        self.server_var.set(settings.server_url)
        self.topic_var.set(settings.push_topic)
        self.count_var.set(str(settings.history_retention_count))
        self.days_var.set(str(settings.history_retention_days))
        self.cache_var.set(str(settings.image_cache_mb))
        self.toasts_var.set(bool(settings.toasts_enabled))
        self.hide_preview_var.set(bool(settings.hide_preview_text))
        self.autostart_var.set(bool(settings.autostart_enabled))
        self.tts_var.set(bool(settings.tts_enabled))
        self.tts_provider_var.set(
            PROVIDER_LABELS.get(settings.tts_provider, PROVIDER_LABELS[DEFAULT_TTS_PROVIDER])
        )
        self.tts_rate_var.set(str(settings.tts_rate))
        self.tts_volume_var.set(str(settings.tts_volume))
        self.tts_body_var.set(bool(settings.tts_include_body))
        self.silence_var.set("Off")
        if settings.silence_until and settings.silence_until > now_ms():
            self.silence_var.set("For 1 hour")

    def _candidate(self) -> dict:
        silence_choice = self.silence_var.get()
        silence_until = None
        if silence_choice in SILENCE_CHOICES and SILENCE_CHOICES[silence_choice]:
            silence_until = now_ms() + SILENCE_CHOICES[silence_choice]
        return {
            "server_url": validate_server_url(self.server_var.get()),
            "push_topic": self.topic_var.get().strip(),
            "history_retention_count": int(self.count_var.get()),
            "history_retention_days": int(self.days_var.get()),
            "image_cache_mb": int(self.cache_var.get()),
            "toasts_enabled": bool(self.toasts_var.get()),
            "hide_preview_text": bool(self.hide_preview_var.get()),
            "tts_enabled": bool(self.tts_var.get()),
            "tts_provider": PROVIDER_LABEL_TO_NAME.get(
                self.tts_provider_var.get(), DEFAULT_TTS_PROVIDER
            ),
            "tts_include_body": bool(self.tts_body_var.get()),
            "tts_rate": int(self.tts_rate_var.get()),
            "tts_volume": int(self.tts_volume_var.get()),
            "silence_until": silence_until,
            "autostart_enabled": bool(self.autostart_var.get()),
            "log_level": self.settings.log_level,
            "last_connected_at": self.settings.last_connected_at,
        }

    # -- actions ----------------------------------------------------------- #
    def _save(self) -> None:
        try:
            candidate = self._candidate()
        except ValueError as exc:
            messagebox.showerror("Invalid setting", str(exc), parent=self.window)
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
            "the autostart entry and the barkpc: registration.\n\nContinue?",
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
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass
        if self.on_closed is not None:
            self.on_closed()


def settings_field_names() -> list[str]:
    return [item.name for item in fields(Settings)]
