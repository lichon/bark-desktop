"""Full-size image viewer with fit-to-window, zoom, save-as and copy."""

from __future__ import annotations

import logging
import os
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

logger = logging.getLogger("bark_pc.ui.image_viewer")

ZOOM_MIN = 0.25
ZOOM_MAX = 4.0
ZOOM_STEP = 1.25


class ImageViewer:
    """A fixed-size Toplevel that displays one cached image."""

    def __init__(self, master, *, path: str | Path, title: str = "Image"):
        self.path = Path(path)
        self.window = tk.Toplevel(master)
        self.window.title(f"Bark — {title}")
        self.window.geometry("820x620")
        self.window.resizable(False, False)
        self.zoom = 1.0
        self._photo = None
        self._image = None
        self._build()
        self._load()

    def _build(self) -> None:
        toolbar = ttk.Frame(self.window, padding=(8, 6))
        toolbar.pack(side="top", fill="x")
        ttk.Button(toolbar, text="Fit", command=self.fit).pack(side="left")
        ttk.Button(toolbar, text="−", width=3, command=lambda: self._zoom_by(1 / ZOOM_STEP)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(toolbar, text="+", width=3, command=lambda: self._zoom_by(ZOOM_STEP)).pack(
            side="left", padx=(4, 0)
        )
        self.zoom_label = ttk.Label(toolbar, text="100 %", style="Muted.TLabel")
        self.zoom_label.pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="Save as…", command=self.save_as).pack(side="right")
        ttk.Button(toolbar, text="Copy", command=self.copy_to_clipboard).pack(
            side="right", padx=(0, 6)
        )
        ttk.Button(toolbar, text="Show in folder", command=self.show_in_folder).pack(
            side="right", padx=(0, 6)
        )

        container = ttk.Frame(self.window)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(container, background="#111827", highlightthickness=0)
        scroll_y = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        scroll_x = ttk.Scrollbar(container, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

        self.window.bind("<Escape>", lambda _e: self.window.destroy())

    def _load(self) -> None:
        try:
            from PIL import Image

            self._image = Image.open(self.path)
            self._image.load()
        except Exception as exc:
            logger.info("cannot open image %s: %s", self.path, exc)
            messagebox.showerror(
                "Image unavailable", f"Could not open the image:\n{exc}", parent=self.window
            )
            self.window.destroy()
            return
        self.fit()

    # -- zoom / fit -------------------------------------------------------- #
    def fit(self) -> None:
        if self._image is None:
            return
        self.canvas.update_idletasks()
        width = max(200, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        ratio = min(width / self._image.width, height / self._image.height)
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, ratio))
        self._redraw()

    def _zoom_by(self, factor: float) -> None:
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        self._redraw()

    def _redraw(self) -> None:
        if self._image is None:
            return
        try:
            from PIL import Image, ImageTk

            width = max(1, int(self._image.width * self.zoom))
            height = max(1, int(self._image.height * self.zoom))
            resized = self._image.resize((width, height), Image.LANCZOS)
            self._photo = ImageTk.PhotoImage(resized)
            self.canvas.delete("all")
            self.canvas.create_image(width // 2, height // 2, image=self._photo)
            self.canvas.configure(scrollregion=(0, 0, width, height))
            self.zoom_label.configure(text=f"{int(self.zoom * 100)} %")
        except Exception as exc:  # pragma: no cover - tk image failures
            logger.info("zoom render failed: %s", exc)

    # -- actions ----------------------------------------------------------- #
    def save_as(self) -> None:
        if self._image is None:
            return
        default_name = self._default_name()
        target = filedialog.asksaveasfilename(
            parent=self.window,
            defaultextension=".png",
            initialfile=default_name,
            filetypes=[("PNG image", "*.png"), ("JPEG image", "*.jpg"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            image = self._image
            if target.lower().endswith((".jpg", ".jpeg")) and image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            image.save(target)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.window)

    def copy_to_clipboard(self) -> None:
        if self._image is None:
            return
        try:
            import io

            image = self._image.convert("RGB")
            buffer = io.BytesIO()
            image.save(buffer, format="BMP")
            data = buffer.getvalue()[14:]  # strip the BMP file header -> CF_DIB
            self.window.clipboard_clear()
            self.window.clipboard_append(data, type="CF_DIB")
        except Exception as exc:
            messagebox.showerror("Copy failed", str(exc), parent=self.window)

    def show_in_folder(self) -> None:
        try:
            os.startfile(str(self.path.parent))  # noqa: S606 - Windows shell open
        except Exception as exc:
            logger.info("could not open the containing folder: %s", exc)

    def _default_name(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = (self.window.title().replace("Bark — ", "") or "image").strip()
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "_" for ch in stem)[:40]
        return f"{safe.strip() or 'image'}-{stamp}.png"

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass


def open_viewer(master, path: str | Path, title: str = "Image") -> ImageViewer:
    return ImageViewer(master, path=path, title=title)
