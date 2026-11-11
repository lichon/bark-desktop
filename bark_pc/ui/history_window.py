"""History window: newest-first list plus a detail pane.

Deliberately plain ttk: a fixed-width list pane on the left (about half the
window) and a keyboard-navigable detail pane on the right.  The list columns
share the pane 20 % title / 60 % content / 20 % time.  All data comes from the
repository; the window never calls the network.
"""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from ..core.models import (
    BODY_MAX_STORED_BYTES,
    CONNECTION_STATUS_LABELS,
    ConnectionStatus,
    ImageState,
    Notification,
)
from .theme import (
    SEND_OFFLINE_BG,
    SEND_OFFLINE_FG,
    SEND_ONLINE_ACTIVE_BG,
    SEND_ONLINE_BG,
)

logger = logging.getLogger("bark_pc.ui.history")

PAGE_SIZE = 100
THUMBNAIL_BOX = (320, 180)
LIST_WIDTH = 442  # half of the fixed 900 px window, minus outer padding
PREVIEW_CHARS = 34  # roughly what the 60 % content column shows at the fixed width
COLUMN_RATIOS = (("title", 0.2), ("body", 0.6), ("time", 0.2))


def preview_text(text: str | None, limit: int = PREVIEW_CHARS) -> str:
    """Collapse whitespace and truncate a body to a single list-cell line."""
    if not text:
        return ""
    single = " ".join(text.split())
    return single if len(single) <= limit else single[: limit - 1] + "…"


def format_time(timestamp_ms: int | None) -> str:
    if not timestamp_ms:
        return ""
    try:
        return datetime.fromtimestamp(timestamp_ms / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError, OverflowError):  # pragma: no cover - defensive
        return ""


def format_time_compact(timestamp_ms: int | None) -> str:
    """Short timestamp for the 20 % time column (full value lives in detail)."""
    if not timestamp_ms:
        return ""
    try:
        moment = datetime.fromtimestamp(timestamp_ms / 1000.0)
    except (OSError, ValueError, OverflowError):  # pragma: no cover - defensive
        return ""
    today = datetime.now()
    if moment.date() == today.date():
        return moment.strftime("%H:%M")
    if moment.year == today.year:
        return moment.strftime("%m-%d")
    return moment.strftime("%y-%m-%d")


class HistoryWindow:
    """Owns a `Toplevel` (or a frame inside one when embedded)."""

    def __init__(
        self,
        master,
        *,
        db,
        callbacks: dict | None = None,
        factor: float = 1.0,
        image_store=None,
    ):
        self.db = db
        self.callbacks = callbacks or {}
        self.factor = factor
        self.image_store = image_store
        self.window = tk.Toplevel(master)
        self.window.title("Bark")
        self.window.geometry("900x560")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.hide)

        self._offset = 0
        self._rows: dict[str, Notification] = {}
        self._selected: Notification | None = None
        self._thumb_photo = None
        self._build()
        self.refresh()

    # -- construction ------------------------------------------------------ #
    def _build(self) -> None:
        toolbar = ttk.Frame(self.window, padding=(8, 6))
        toolbar.pack(side="top", fill="x")
        self.settings_button = ttk.Button(
            toolbar, text="Settings", command=self._open_settings
        )
        self.settings_button.pack(side="left")
        ttk.Button(toolbar, text="Clear all", command=self._clear_all).pack(
            side="left", padx=(6, 0)
        )
        self.status_label = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.status_label.pack(side="right")

        body = ttk.Frame(self.window)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(body, width=LIST_WIDTH)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        list_frame = ttk.Frame(left)
        list_frame.grid(row=0, column=0, sticky="nsew")
        columns = ("title", "body", "time")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("title", text="Title")
        self.tree.heading("body", text="Content")
        self.tree.heading("time", text="Time")
        self.tree.column("title", width=88, anchor="w", stretch=False)
        self.tree.column("body", width=254, anchor="w", stretch=False)
        self.tree.column("time", width=84, anchor="e", stretch=False)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Configure>", self._on_tree_configure, add="+")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<ButtonRelease-1>", self._on_user_pick)
        self.tree.bind("<Return>", self._on_user_pick)
        self.tree.bind("<Delete>", lambda _e: self._delete_selected())
        self.tree.bind("<Up>", lambda _e: self._navigate(-1))
        self.tree.bind("<Down>", lambda _e: self._navigate(1))

        self.more_button = ttk.Button(left, text="Load more", command=self.load_more)
        self.more_button.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.more_button.grid_remove()

        right = ttk.Frame(body, padding=(10, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        self.detail_title = ttk.Label(
            right, text="No notification selected", style="Title.TLabel", wraplength=320
        )
        self.detail_title.pack(anchor="w")
        self.detail_meta = ttk.Label(right, text="", style="Muted.TLabel", wraplength=320)
        self.detail_meta.pack(anchor="w", pady=(2, 2))
        self.detail_group = ttk.Label(right, text="", style="Muted.TLabel", wraplength=320)
        self.detail_group.pack(anchor="w", pady=(0, 6))
        self.thumb_label = ttk.Label(right, text="", anchor="center")
        self.thumb_label.pack(fill="x", pady=(0, 6))
        self.image_state_label = ttk.Label(right, text="", style="Muted.TLabel")
        self.image_state_label.pack(anchor="w")
        self.retry_button = ttk.Button(right, text="Retry image", command=self._retry_image)

        self.body_text = tk.Text(right, height=10, wrap="word", relief="flat", borderwidth=1)
        self.body_text.pack(fill="both", expand=True)
        self.body_text.configure(state="disabled")

        actions = ttk.Frame(right)
        actions.pack(fill="x", pady=(6, 0))
        ttk.Button(actions, text="Copy content", command=self._copy).pack(side="left")
        # Flush with the composer's Send button so both sit on the same right edge.
        self.delete_button = ttk.Button(
            actions, text="Delete", command=self._delete_selected
        )
        self.delete_button.pack(side="right")

        # Compose bar: send a message this machine writes.  The settings decide
        # whether it goes to the Supabase Realtime room, a Bark URL, or both;
        # the button stays disabled until at least one route is usable.
        composer = ttk.Frame(self.window, padding=(8, 0, 8, 8))
        composer.pack(side="bottom", fill="x")
        # Single row grid: both cells stretch vertically so the one-line entry
        # ends up exactly as tall as the Send button.
        composer.columnconfigure(0, weight=1)
        self.message_var = tk.StringVar()
        self.message_entry = ttk.Entry(composer, textvariable=self.message_var)
        self.message_entry.grid(row=0, column=0, sticky="nsew")
        self.message_entry.bind("<Return>", self._on_composer_return)
        self.message_entry.bind("<KP_Enter>", self._on_composer_return)
        # A classic tk button, not ttk: the native Windows theme ignores a ttk
        # button's background, and this button must carry the tray icon colour.
        self.send_button = tk.Button(
            composer,
            text="Send",
            command=self._send_message,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=14,
            pady=3,
            foreground="#ffffff",
            background=SEND_OFFLINE_BG,
            activeforeground="#ffffff",
            activebackground=SEND_ONLINE_ACTIVE_BG,
            disabledforeground=SEND_OFFLINE_FG,
        )
        self.send_button.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.send_button.configure(state="disabled")

    # -- data -------------------------------------------------------------- #
    def refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._rows.clear()
        self._offset = 0
        self._load_page()
        self._update_status()

    def _load_page(self, *, select_first: bool = True) -> None:
        rows = self.db.list_notifications(limit=PAGE_SIZE, offset=self._offset)
        first_item: str | None = None
        for notification in rows:
            item = self._insert_row(notification)
            if first_item is None:
                first_item = item
        if select_first and self._selected is None and first_item is not None:
            self.tree.selection_set(first_item)
        self._offset += len(rows)
        if len(rows) == PAGE_SIZE:
            self.more_button.grid()
        else:
            self.more_button.grid_remove()

    def load_more(self) -> None:
        self._load_page()

    def _on_tree_configure(self, event) -> None:
        """Keep the columns at 20 / 60 / 20 of the list width."""
        width = event.width
        if width <= 1 or width == getattr(self, "_columns_width", None):
            return
        self._columns_width = width
        remaining = width
        for index, (column, ratio) in enumerate(COLUMN_RATIOS):
            if index == len(COLUMN_RATIOS) - 1:
                pixels = remaining
            else:
                pixels = int(round(width * ratio))
                remaining -= pixels
            self.tree.column(column, width=max(1, pixels))

    def _insert_row(self, notification: Notification) -> str:
        item = self.tree.insert(
            "",
            "end",
            values=(
                ("• " if notification.read_at is None else "")
                + (notification.display_title or "(no title)"),
                preview_text(notification.body),
                format_time_compact(notification.received_at),
            ),
            tags=("unread",) if notification.read_at is None else (),
        )
        self._rows[item] = notification
        self.tree.tag_configure("unread", font=("Segoe UI", 10, "bold"))
        return item

    def refresh_status(self) -> None:
        """Re-read the connection status shown in the toolbar."""
        self._update_status()

    def _update_status(self) -> None:
        text = self._status_text()
        notice = self._notice()
        if notice:
            text = f"{text} · {notice}" if text else notice
        self.status_label.configure(text=text)
        self._update_send_state()

    def _open_settings(self) -> None:
        """Open the settings dialog through the controller."""
        callback = self.callbacks.get("on_open_settings")
        if callback is None:
            return
        try:
            callback()
        except Exception:  # pragma: no cover - UI callback must not crash
            logger.exception("open settings failed")

    def _status_text(self) -> str:
        """Server host plus connection state from the controller.

        The host keeps the label unambiguous when several Bark servers are in
        use; it is omitted while no link is stored (the state alone is shown).
        """
        label = ""
        getter = self.callbacks.get("connection_status")
        if getter is not None:
            try:
                text = getter()
                if text:
                    label = str(text)
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("connection_status callback failed")
        if not label:
            label = CONNECTION_STATUS_LABELS[ConnectionStatus.STARTING]
        host = self._server_host()
        return f"{host} · {label}" if host else label

    def _server_host(self) -> str:
        """Bark server host (`host:port`) from the stored link, or empty."""
        try:
            link = self.db.get_link()
        except Exception:  # pragma: no cover - repository isolation
            logger.exception("reading the link failed")
            return ""
        if link is None or not link.server_url:
            return ""
        try:
            return urlsplit(link.server_url).netloc or link.server_url
        except ValueError:  # pragma: no cover - defensive
            return link.server_url

    def _notice(self) -> str:
        """Optional trailing hint from the controller (e.g. failed toasts)."""
        getter = self.callbacks.get("toast_notice")
        if getter is None:
            return ""
        try:
            return getter() or ""
        except Exception:  # pragma: no cover - callback isolation
            logger.exception("toast_notice callback failed")
            return ""

    # -- compose bar ------------------------------------------------------- #
    def _can_send(self) -> bool:
        """True when the configured self-message route can accept a message.

        The controller answers this (`can_send_message`); older wiring that only
        knew about the Realtime transport is still honoured.
        """
        getter = self.callbacks.get("can_send_message")
        if getter is None:
            getter = self.callbacks.get("supabase_connected")
        if getter is None:
            return False
        try:
            return bool(getter())
        except Exception:  # pragma: no cover - callback isolation
            logger.exception("can_send_message callback failed")
            return False

    def _update_send_state(self) -> None:
        """Enable Send while at least one configured route is usable.

        The button wears the tray icon's online colour while a message can go
        out and the icon's offline colour while it cannot.
        """
        can_send = self._can_send()
        self.send_button.configure(
            state="normal" if can_send else "disabled",
            background=SEND_ONLINE_BG if can_send else SEND_OFFLINE_BG,
            activebackground=SEND_ONLINE_ACTIVE_BG if can_send else SEND_OFFLINE_BG,
            disabledforeground=SEND_OFFLINE_FG,
        )

    def _on_composer_return(self, _event=None) -> str:
        self._send_message()
        return "break"

    def _send_message(self) -> None:
        text = self.message_var.get().strip()
        if not text or not self._can_send():
            return
        callback = self.callbacks.get("on_send_message")
        if callback is None:
            return
        try:
            sent = callback(text)
        except Exception:  # pragma: no cover - callback isolation
            logger.exception("on_send_message callback failed")
            return
        if sent is not False:
            self.message_var.set("")
            self.message_entry.focus_set()

    # -- selection --------------------------------------------------------- #
    def _on_select(self, _event=None) -> None:
        """Show the selected row; marking it read is the user's job.

        `<<TreeviewSelect>>` also fires for selections the window makes itself
        (refresh, delete, navigation), so it must never consume the unread flag.
        """
        selection = self.tree.selection()
        if not selection:
            return
        notification = self._rows.get(selection[0])
        if notification is None:
            return
        self._selected = notification
        self._render_detail(notification)

    def _on_user_pick(self, _event=None) -> None:
        """Click or Return on a row: that is an explicit read."""
        selection = self.tree.selection()
        if selection:
            self._mark_read(selection[0])

    def _mark_read(self, item: str) -> None:
        notification = self._rows.get(item)
        if notification is None or notification.id is None:
            return
        if notification.read_at is not None:
            return
        self.db.mark_read(notification.id)
        notification.read_at = 1
        self.tree.set(item, "title", notification.display_title or "(no title)")
        self.tree.item(item, tags=())
        callback = self.callbacks.get("on_read")
        if callback:
            callback(notification.id)

    def select_notification(self, notification_id: int) -> None:
        for item, notification in self._rows.items():
            if notification.id == notification_id:
                self.tree.selection_set(item)
                self.tree.see(item)
                self._mark_read(item)
                return
        notification = self.db.get_notification(notification_id)
        if notification is not None:
            item = self._insert_row(notification)
            self.tree.selection_set(item)
            self.tree.see(item)
            self._mark_read(item)

    def _navigate(self, delta: int) -> None:
        items = list(self.tree.get_children())
        if not items:
            return
        selection = self.tree.selection()
        index = items.index(selection[0]) if selection else 0
        index = max(0, min(len(items) - 1, index + delta))
        self.tree.selection_set(items[index])
        self.tree.see(items[index])
        self._mark_read(items[index])

    def _render_detail(self, notification: Notification) -> None:
        self.detail_title.configure(text=notification.display_title or "(no title)")
        meta_parts = [format_time(notification.received_at)]
        if notification.level:
            meta_parts.append(notification.level)
        self.detail_meta.configure(text=" · ".join(part for part in meta_parts if part))
        self.detail_group.configure(
            text=f"Group: {notification.group_name}" if notification.group_name else ""
        )
        self.body_text.configure(state="normal")
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", (notification.body or "")[:BODY_MAX_STORED_BYTES])
        self.body_text.configure(state="disabled")
        self._render_thumbnail(notification)

    def _render_thumbnail(self, notification: Notification) -> None:
        self.thumb_label.configure(image="", text="")
        self._thumb_photo = None
        self.retry_button.pack_forget()
        if not notification.image_url:
            self.image_state_label.configure(text="")
            return
        entry = None
        if self.image_store is not None:
            entry = self.image_store.get(notification.image_url)
        if entry is None:
            self.image_state_label.configure(text="Image pending…")
            return
        if entry.state != ImageState.CACHED:
            label = {
                ImageState.PENDING: "Image pending…",
                ImageState.UNAVAILABLE: "Image unavailable",
                ImageState.TOO_LARGE: "Image too large (over 10 MB)",
                ImageState.NOT_IMAGE: "Not an image",
            }.get(entry.state, "Image unavailable")
            self.image_state_label.configure(text=label)
            if entry.state == ImageState.UNAVAILABLE:
                self.retry_button.pack(anchor="w")
            return
        path = self.image_store.thumbnail_path(entry) if self.image_store else None
        if path is None:
            self.image_state_label.configure(text="Image file missing")
            return
        try:
            from PIL import Image, ImageTk

            with Image.open(path) as image:
                image.thumbnail(THUMBNAIL_BOX)
                self._thumb_photo = ImageTk.PhotoImage(image.copy())
            self.thumb_label.configure(image=self._thumb_photo)
            self.image_state_label.configure(
                text=f"{entry.width}×{entry.height} · {entry.bytes // 1024} KB"
            )
        except Exception as exc:  # pragma: no cover - tk image failures
            logger.info("thumbnail render failed: %s", exc)
            self.image_state_label.configure(text="Image preview unavailable")

    # -- actions ----------------------------------------------------------- #
    def _clear_all(self) -> None:
        if not messagebox.askyesno(
            "Clear all", "Delete all locally stored notifications?", parent=self.window
        ):
            return
        callback = self.callbacks.get("on_clear_all")
        if callback:
            callback()
        else:
            self.db.clear_history()
        self.refresh()

    def _delete_selected(self) -> None:
        if self._selected is None or self._selected.id is None:
            return
        callback = self.callbacks.get("on_delete")
        if callback and not callback(self._selected.id):
            return
        self.db.delete_notification(self._selected.id)
        index = 0
        for item, notification in list(self._rows.items()):
            if notification.id == self._selected.id:
                index = self.tree.index(item)
                self.tree.delete(item)
                self._rows.pop(item, None)
                break
        self._selected = None
        self._select_after_delete(index)

    def _select_after_delete(self, index: int) -> None:
        """Select the row that took the deleted one's place, else the previous."""
        items = list(self.tree.get_children())
        if index >= len(items):
            # The tail of the loaded page was deleted: pull the next row in so
            # the gap is filled instead of leaving the pane blank.
            self._offset = len(items)
            self._load_page(select_first=False)
            items = list(self.tree.get_children())
        if not items:
            self._clear_detail()
            return
        target = items[max(0, min(len(items) - 1, index))]
        self.tree.selection_set(target)
        self.tree.focus(target)
        self.tree.see(target)
        # Update the detail pane immediately instead of waiting for the
        # queued <<TreeviewSelect>> event.
        self._on_select()

    def _copy(self) -> None:
        if self._selected is None:
            return
        text = self._selected.body or self._selected.display_title
        callback = self.callbacks.get("on_copy_text")
        if callback:
            callback(text)

    def _retry_image(self) -> None:
        if self._selected is None:
            return
        callback = self.callbacks.get("on_retry_image")
        if callback:
            callback(self._selected.image_url)

    def _clear_detail(self) -> None:
        self.detail_title.configure(text="No notification selected")
        self.detail_meta.configure(text="")
        self.detail_group.configure(text="")
        self.body_text.configure(state="normal")
        self.body_text.delete("1.0", "end")
        self.body_text.configure(state="disabled")
        self.thumb_label.configure(image="")
        self.image_state_label.configure(text="")

    # -- window ------------------------------------------------------------ #
    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.refresh()

    def hide(self) -> None:
        self.window.withdraw()

    def destroy(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass
