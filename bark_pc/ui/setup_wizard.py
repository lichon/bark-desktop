"""First-run setup wizard: server choice → activation → registration → key/QR.

There is no Apple account step anywhere in this flow: activation is anonymous.
Network work runs on a worker thread and reports progress through `after(0, …)`
so the Tk event loop never blocks.
"""

from __future__ import annotations

import io
import logging
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

logger = logging.getLogger("bark_pc.ui.setup")

PUBLIC_SERVER = "https://api.day.app"
QR_PIXELS = 240
ACCOUNT_WARNING = "No Apple ID, password or 2FA is used. Activation is anonymous."


@dataclass
class SetupResult:
    ok: bool
    server_url: str = ""
    device_key: str = ""
    push_url: str = ""
    error: str | None = None


class SetupWizard:
    """Three-step wizard; `controller` supplies the blocking setup operations."""

    def __init__(
        self,
        master,
        *,
        controller,
        current_server: str = "",
        on_finished=None,
        embedded: bool = False,
    ):
        self.controller = controller
        self.on_finished = on_finished
        self.window = tk.Toplevel(master)
        self.window.title("Bark Desktop Client — Setup")
        self.window.geometry("640x520")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.result: SetupResult | None = None
        self._busy = False
        self._qr_photo = None
        self._build(current_server)

    # -- layout ------------------------------------------------------------ #
    def _build(self, current_server: str) -> None:
        self.header = ttk.Label(
            self.window, text="Step 1 of 3 — Choose your server", style="Title.TLabel"
        )
        self.header.pack(anchor="w", padx=16, pady=(14, 4))
        self.body = ttk.Frame(self.window, padding=(16, 8))
        self.body.pack(fill="both", expand=True)

        # Step 1
        self.step1 = ttk.Frame(self.body)
        ttk.Label(
            self.step1,
            text="Bark pushes are delivered through a Bark-compatible server.\n"
            "Use the public server or the address of your self-hosted server.",
            wraplength=560,
        ).pack(anchor="w", pady=(0, 10))
        self.server_choice = tk.StringVar(value="public")
        ttk.Radiobutton(
            self.step1,
            text=f"Public server ({PUBLIC_SERVER})",
            value="public",
            variable=self.server_choice,
            command=self._toggle_custom,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Radiobutton(
            self.step1,
            text="Self-hosted or custom server",
            value="custom",
            variable=self.server_choice,
            command=self._toggle_custom,
        ).pack(anchor="w", pady=(0, 6))
        self.server_entry = ttk.Entry(self.step1, width=52)
        self.server_entry.pack(anchor="w", padx=(22, 0), pady=(0, 10))
        if current_server and current_server != PUBLIC_SERVER:
            self.server_choice.set("custom")
            self.server_entry.insert(0, current_server)
        ttk.Label(self.step1, text=ACCOUNT_WARNING, style="Muted.TLabel").pack(
            anchor="w", pady=(8, 0)
        )
        buttons1 = ttk.Frame(self.step1)
        buttons1.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(buttons1, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(
            buttons1, text="Continue", style="Accent.TButton", command=self._start_setup
        ).pack(side="right", padx=(0, 8))

        # Step 2
        self.step2 = ttk.Frame(self.body)
        ttk.Label(self.step2, text="Setting up your device key", style="Subtitle.TLabel").pack(
            anchor="w"
        )
        self.progress_label = ttk.Label(self.step2, text="Starting…", wraplength=560)
        self.progress_label.pack(anchor="w", pady=(10, 4))
        self.progress = ttk.Progressbar(self.step2, mode="indeterminate", length=520)
        self.progress.pack(anchor="w", pady=(4, 10))
        self.error_label = ttk.Label(self.step2, text="", style="Danger.TLabel", wraplength=560)
        self.error_label.pack(anchor="w")
        buttons2 = ttk.Frame(self.step2)
        buttons2.pack(fill="x", side="bottom", pady=(16, 0))
        self.back_button = ttk.Button(buttons2, text="Change server", command=self._back_to_step1)
        self.back_button.pack(side="right")
        self.retry_button = ttk.Button(buttons2, text="Retry", command=self._start_setup)
        self.retry_button.pack(side="right", padx=(0, 8))

        # Step 3
        self.step3 = ttk.Frame(self.body)
        ttk.Label(self.step3, text="You're linked", style="Subtitle.TLabel").pack(anchor="w")
        self.key_label = ttk.Label(self.step3, text="", font=("Consolas", 11))
        self.key_label.pack(anchor="w", pady=(8, 2))
        self.server_label = ttk.Label(self.step3, text="", style="Muted.TLabel", wraplength=340)
        self.server_label.pack(anchor="w")
        self.url_label = ttk.Label(self.step3, text="", wraplength=340)
        self.url_label.pack(anchor="w", pady=(6, 2))
        self.qr_label = ttk.Label(self.step3, text="")
        self.qr_label.pack(anchor="w", pady=(8, 4))
        copy_row = ttk.Frame(self.step3)
        copy_row.pack(anchor="w", pady=(4, 0))
        ttk.Button(copy_row, text="Copy URL", command=self._copy_url).pack(side="left")
        ttk.Button(copy_row, text="Save QR as PNG", command=self._save_qr).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(copy_row, text="Send test notification", command=self._self_test).pack(
            side="left", padx=(8, 0)
        )
        self.test_label = ttk.Label(self.step3, text="", style="Muted.TLabel", wraplength=560)
        self.test_label.pack(anchor="w", pady=(8, 0))
        buttons3 = ttk.Frame(self.step3)
        buttons3.pack(fill="x", side="bottom", pady=(16, 0))
        ttk.Button(buttons3, text="Done", style="Accent.TButton", command=self.close).pack(
            side="right"
        )

        self._toggle_custom()
        self._show(self.step1, "Step 1 of 3 — Choose your server")

    def _show(self, frame, header: str) -> None:
        for candidate in (self.step1, self.step2, self.step3):
            candidate.pack_forget()
        frame.pack(fill="both", expand=True)
        self.header.configure(text=header)

    def _toggle_custom(self) -> None:
        state = "normal" if self.server_choice.get() == "custom" else "disabled"
        self.server_entry.configure(state=state)
        if self.server_choice.get() == "public":
            self.server_entry.delete(0, "end")
            self.server_entry.insert(0, PUBLIC_SERVER)

    # -- step 2 ------------------------------------------------------------ #
    def _selected_server(self) -> str:
        if self.server_choice.get() == "public":
            return PUBLIC_SERVER
        return self.server_entry.get().strip() or PUBLIC_SERVER

    def _start_setup(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._show(self.step2, "Step 2 of 3 — Activating and registering")
        self.error_label.configure(text="")
        self.retry_button.pack_forget()
        self.progress.start(12)
        server = self._selected_server()

        def progress(message: str) -> None:
            self.window.after(0, lambda: self.progress_label.configure(text=message))

        def worker() -> None:
            try:
                result = self.controller.run_setup(server, progress)
            except Exception as exc:  # surfaced to the user, not raised into Tk
                logger.info("setup failed: %s", exc)
                logger.debug("setup failure details", exc_info=True)
                message = str(exc)
                self.window.after(0, lambda: self._setup_failed(message))
                return
            self.window.after(0, lambda: self._setup_succeeded(result))

        threading.Thread(target=worker, name="bark-setup", daemon=True).start()

    def _setup_failed(self, message: str) -> None:
        self._busy = False
        self.progress.stop()
        self.progress_label.configure(text="Setup did not complete.")
        self.error_label.configure(text=message)
        self.retry_button.pack(side="right", padx=(0, 8))

    def _setup_succeeded(self, result: SetupResult) -> None:
        self._busy = False
        self.progress.stop()
        self.result = result
        if not result.ok:
            self._setup_failed(result.error or "Setup failed.")
            return
        self.key_label.configure(text=f"Device key: {result.device_key}")
        self.server_label.configure(text=f"Server: {result.server_url}")
        self.url_label.configure(text=f"Push URL: {result.push_url}")
        self._render_qr(result.push_url)
        self._show(self.step3, "Step 3 of 3 — Your push URL")
        if self.on_finished is not None:
            try:
                self.on_finished(result)
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("on_finished callback failed")

    def _back_to_step1(self) -> None:
        if self._busy:
            return
        self.error_label.configure(text="")
        self._show(self.step1, "Step 1 of 3 — Choose your server")

    # -- step 3 ------------------------------------------------------------ #
    def _render_qr(self, payload: str) -> None:
        if not payload:
            self.qr_label.configure(text="")
            return
        try:
            import segno
            from PIL import Image, ImageTk

            qr = segno.make(payload, error="m")
            buffer = io.BytesIO()
            qr.save(buffer, kind="png", scale=6, border=2)
            buffer.seek(0)
            with Image.open(buffer) as image:
                image = image.convert("RGB")
                image.thumbnail((QR_PIXELS, QR_PIXELS))
                self._qr_photo = ImageTk.PhotoImage(image.copy())
            self.qr_label.configure(image=self._qr_photo)
        except Exception as exc:  # pragma: no cover - QR/rendering failures
            logger.info("QR rendering failed: %s", exc)
            self.qr_label.configure(text="QR code unavailable; copy the URL instead.")

    def _copy_url(self) -> None:
        if self.result is None:
            return
        self.window.clipboard_clear()
        self.window.clipboard_append(self.result.push_url)
        self.test_label.configure(text="Push URL copied to the clipboard.")

    def _save_qr(self) -> None:
        if self.result is None or not self.result.push_url:
            return
        from tkinter import filedialog

        target = filedialog.asksaveasfilename(
            parent=self.window,
            defaultextension=".png",
            initialfile="bark-push-qr.png",
            filetypes=[("PNG image", "*.png")],
        )
        if not target:
            return
        try:
            import segno

            segno.make(self.result.push_url, error="m").save(target, scale=8, border=2)
            self.test_label.configure(text=f"QR saved to {target}")
        except Exception as exc:
            self.test_label.configure(text=f"Could not save the QR code: {exc}")

    def _self_test(self) -> None:
        if self.result is None:
            return
        self.test_label.configure(text="Sending a test push…")
        self._busy = True

        def worker() -> None:
            try:
                accepted, received, message = self.controller.run_self_test()
            except Exception as exc:
                accepted, received, message = False, False, str(exc)
            self.window.after(0, lambda: self._self_test_done(accepted, received, message))

        threading.Thread(target=worker, name="bark-self-test", daemon=True).start()

    def _self_test_done(self, accepted: bool, received: bool, message: str) -> None:
        self._busy = False
        if accepted and received:
            text = "Server accepted the push and it was received on this device."
        elif accepted:
            text = (
                "Server accepted the push, but it was not received within 30 s. "
                "The server may be using a different push topic."
            )
        else:
            text = f"The push was not accepted: {message}"
        self.test_label.configure(text=text)

    # -- window ------------------------------------------------------------ #
    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass

    def cancel(self) -> None:
        self.close()


def setup_result_ok(server_url: str, device_key: str, push_url: str) -> SetupResult:
    return SetupResult(ok=True, server_url=server_url, device_key=device_key, push_url=push_url)
