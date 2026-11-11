"""Webhook routing configuration: list rules and edit one in a modal dialog."""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import messagebox, ttk

from ..push.routing import (
    BODY_FORMAT_JSON,
    BODY_FORMAT_LABELS,
    BODY_FORMATS,
    BODY_LABEL_TO_FORMAT,
    DEFAULT_BODY_FORMAT,
    DEFAULT_WEBHOOK_METHOD,
    MAX_ROUTE_NAME_LENGTH,
    WEBHOOK_METHODS,
    RouteStore,
    WebhookRoute,
    format_header_lines,
    parse_header_lines,
)

logger = logging.getLogger("bark_pc.ui.routing")


class RoutingDialog:
    """Lists the inbox webhook rules; edits stay in memory until *Save*."""

    def __init__(self, master, *, route_store: RouteStore, on_saved=None, on_closed=None):
        self.store = route_store
        self.on_saved = on_saved
        self.on_closed = on_closed
        self.routes: list[WebhookRoute] = list(route_store.routes)
        self.window = tk.Toplevel(master)
        self.window.title("Bark Desktop Client — Message routing")
        self.window.minsize(720, 300)
        if master is not None and master.winfo_viewable():
            self.window.transient(master)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh()
        self._fit_to_content()

    # -- layout ------------------------------------------------------------ #
    def _build(self) -> None:
        container = ttk.Frame(self.window, padding=14)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="Webhook routing", style="Subtitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            container,
            text=(
                "Forward incoming messages to any HTTP webhook. A rule fires when the "
                "message group and the title condition both match (an empty condition "
                "matches every message). Group matches the push's group (or the "
                "Realtime room name); title matches the title or subtitle "
                "(case-insensitive)."
            ),
            style="Muted.TLabel",
            wraplength=620,
            justify="left",
        ).pack(anchor="w", pady=(2, 8))

        table = ttk.Frame(container)
        table.pack(fill="both", expand=True)
        columns = ("enabled", "match", "method", "body", "url")
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="browse", height=9
        )
        self.tree.heading("enabled", text="On")
        self.tree.heading("match", text="Matches")
        self.tree.heading("method", text="Method")
        self.tree.heading("body", text="Body")
        self.tree.heading("url", text="Webhook")
        self.tree.column("enabled", width=42, anchor="center", stretch=False)
        self.tree.column("match", width=220, anchor="w", stretch=False)
        self.tree.column("method", width=72, anchor="w", stretch=False)
        self.tree.column("body", width=110, anchor="w", stretch=False)
        self.tree.column("url", width=260, anchor="w")
        self.tree.tag_configure("disabled", foreground="#9ca3af")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _event: self._edit_selected())
        self.tree.bind("<Return>", lambda _event: self._edit_selected())
        self.tree.bind("<Delete>", lambda _event: self._remove_selected())

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="Add…", command=self._add).pack(side="left")
        ttk.Button(actions, text="Edit…", command=self._edit_selected).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="Remove", command=self._remove_selected).pack(
            side="left", padx=(6, 0)
        )

        self.status_label = ttk.Label(
            container, text="", style="Muted.TLabel", wraplength=620
        )
        self.status_label.pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(container)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(
            buttons, text="Save", style="Accent.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))

    def _fit_to_content(self) -> None:
        self.window.update_idletasks()
        width = max(680, self.window.winfo_reqwidth())
        height = min(self.window.winfo_reqheight(), self.window.winfo_screenheight() - 80)
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    # -- data -------------------------------------------------------------- #
    def _refresh(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, route in enumerate(self.routes):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    "Yes" if route.enabled else "No",
                    route.match_summary,
                    route.method,
                    self._body_summary(route),
                    route.url,
                ),
                tags=() if route.enabled else ("disabled",),
            )
        if self.routes:
            self.status_label.configure(
                text=f"{len(self.routes)} rule(s). Changes apply when you press Save."
            )
        else:
            self.status_label.configure(
                text="No rules yet. Add one to forward incoming messages."
            )

    @staticmethod
    def _body_summary(route: WebhookRoute) -> str:
        if not route.sends_body:
            return "—"
        return BODY_FORMAT_LABELS.get(route.body_format, route.body_format)

    def _selected_index(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            index = int(selection[0])
        except (TypeError, ValueError):
            return None
        return index if 0 <= index < len(self.routes) else None

    def _select(self, index: int) -> None:
        iid = str(index)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.focus(iid)

    # -- actions ----------------------------------------------------------- #
    def _add(self) -> None:
        editor = RuleEditor(self.window, route=None)
        editor.show()
        self.window.wait_window(editor.window)
        if editor.result is None:
            return
        self.routes.append(editor.result)
        self._refresh()
        self._select(len(self.routes) - 1)

    def _edit_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            self.status_label.configure(text="Select a rule first.")
            return
        editor = RuleEditor(self.window, route=self.routes[index])
        editor.show()
        self.window.wait_window(editor.window)
        if editor.result is None:
            return
        self.routes[index] = editor.result
        self._refresh()
        self._select(index)

    def _remove_selected(self) -> None:
        index = self._selected_index()
        if index is None:
            self.status_label.configure(text="Select a rule first.")
            return
        route = self.routes[index]
        if not messagebox.askyesno(
            "Remove rule",
            f"Remove the rule for {route.display_name}?",
            parent=self.window,
        ):
            return
        del self.routes[index]
        self._refresh()
        if self.routes:
            self._select(min(index, len(self.routes) - 1))

    def _save(self) -> None:
        try:
            self.routes = self.store.save(self.routes)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Message routing", str(exc), parent=self.window)
            return
        if self.on_saved is not None:
            try:
                self.on_saved(list(self.routes))
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("routing saved callback failed")
        self.close()

    # -- lifecycle --------------------------------------------------------- #
    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
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


class RuleEditor:
    """Modal add/edit form for one `WebhookRoute`."""

    def __init__(self, master, *, route: WebhookRoute | None = None):
        self.route = route
        self.result: WebhookRoute | None = None
        self.window = tk.Toplevel(master)
        self.window.title("Edit webhook rule" if route else "Add webhook rule")
        self.window.resizable(False, False)
        if master is not None and master.winfo_viewable():
            self.window.transient(master)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._load()
        self._fit_to_content()

    def _build(self) -> None:
        container = ttk.Frame(self.window, padding=14)
        container.pack(fill="both", expand=True)
        container.columnconfigure(1, weight=1)

        self.enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            container, text="Rule enabled", variable=self.enabled_var
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        ttk.Label(container, text="Name (optional)").grid(row=1, column=0, sticky="w")
        self.name_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.name_var, width=48).grid(
            row=1, column=1, sticky="ew", pady=2
        )
        ttk.Label(
            container,
            text=f"At most {MAX_ROUTE_NAME_LENGTH} characters.",
            style="Muted.TLabel",
        ).grid(row=2, column=1, sticky="w")

        ttk.Label(container, text="Message group").grid(
            row=3, column=0, sticky="w", pady=(6, 0)
        )
        self.group_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.group_var, width=48).grid(
            row=3, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Label(
            container,
            text="Matches the push's group or the Realtime room. Empty = any message.",
            style="Muted.TLabel",
        ).grid(row=4, column=1, sticky="w")

        ttk.Label(container, text="Title contains").grid(
            row=5, column=0, sticky="w", pady=(6, 0)
        )
        self.title_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.title_var, width=48).grid(
            row=5, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Label(
            container,
            text="Case-insensitive substring of the title or subtitle. Empty = any.",
            style="Muted.TLabel",
        ).grid(row=6, column=1, sticky="w")

        ttk.Label(container, text="Webhook URL").grid(
            row=9, column=0, sticky="w", pady=(6, 0)
        )
        self.url_var = tk.StringVar()
        ttk.Entry(container, textvariable=self.url_var, width=48).grid(
            row=9, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Label(
            container,
            text=(
                "No fields are added automatically. Use placeholders like "
                "{group}, {title}, {body}."
            ),
            style="Muted.TLabel",
        ).grid(row=10, column=1, sticky="w")

        ttk.Label(container, text="Method").grid(
            row=11, column=0, sticky="w", pady=(6, 0)
        )
        self.method_var = tk.StringVar(value=DEFAULT_WEBHOOK_METHOD)
        ttk.Combobox(
            container,
            textvariable=self.method_var,
            values=list(WEBHOOK_METHODS),
            state="readonly",
            width=10,
        ).grid(row=11, column=1, sticky="w", pady=(6, 0))
        ttk.Label(
            container,
            text="GET sends no body; POST, PUT and PATCH send the body below.",
            style="Muted.TLabel",
        ).grid(row=12, column=1, sticky="w")

        ttk.Label(container, text="Body format").grid(
            row=13, column=0, sticky="w", pady=(6, 0)
        )
        self.body_format_var = tk.StringVar(
            value=BODY_FORMAT_LABELS[DEFAULT_BODY_FORMAT]
        )
        body_format = ttk.Combobox(
            container,
            textvariable=self.body_format_var,
            values=[BODY_FORMAT_LABELS[item] for item in BODY_FORMATS],
            state="readonly",
            width=28,
        )
        body_format.grid(row=13, column=1, sticky="w", pady=(6, 0))
        body_format.bind("<<ComboboxSelected>>", lambda _event: self._update_body_hint())

        ttk.Label(container, text="Body").grid(
            row=14, column=0, sticky="nw", pady=(6, 0)
        )
        self.body_text = tk.Text(
            container, width=48, height=5, wrap="none", relief="solid", borderwidth=1
        )
        self.body_text.grid(row=14, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(
            container,
            text=(
                "Placeholders: {title}, {subtitle}, {display_title}, {body}, "
                "{group}, {url}, {image_url}, {source}, {id}, {remote_id}, "
                "{received_at}, {received_at_iso}, {payload}. Empty = no body."
            ),
            style="Muted.TLabel",
            wraplength=460,
            justify="left",
        ).grid(row=15, column=1, sticky="w")
        self.body_hint = ttk.Label(
            container, text="", style="Muted.TLabel", wraplength=460, justify="left"
        )
        self.body_hint.grid(row=16, column=1, sticky="w")

        ttk.Label(container, text="Headers (optional)").grid(
            row=17, column=0, sticky="nw", pady=(6, 0)
        )
        self.headers_text = tk.Text(
            container, width=48, height=3, wrap="none", relief="solid", borderwidth=1
        )
        self.headers_text.grid(row=17, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(
            container,
            text="One Name: value per line, for example Authorization: Bearer …",
            style="Muted.TLabel",
        ).grid(row=18, column=1, sticky="w")
        self._update_body_hint()

        buttons = ttk.Frame(container)
        buttons.grid(row=19, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(
            buttons, text="Save rule", style="Accent.TButton", command=self._save
        ).pack(side="right", padx=(0, 8))

    def _fit_to_content(self) -> None:
        self.window.update_idletasks()
        width = max(580, self.window.winfo_reqwidth())
        height = min(self.window.winfo_reqheight(), self.window.winfo_screenheight() - 80)
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def _body_format(self) -> str:
        return BODY_LABEL_TO_FORMAT.get(self.body_format_var.get(), DEFAULT_BODY_FORMAT)

    def _update_body_hint(self) -> None:
        if self._body_format() == BODY_FORMAT_JSON:
            text = (
                'Example: {"text": "{display_title}: {body}"}. '
                "Placeholder values are JSON-escaped; the filled body must stay "
                "valid JSON. Use {payload} without quotes for the raw payload."
            )
        else:
            text = (
                "Example: text={display_title}&descr={body}. "
                "Placeholder values are form-encoded."
            )
        self.body_hint.configure(text=text)

    def _load(self) -> None:
        route = self.route
        if route is None:
            self.headers_text.insert("1.0", "")
            return
        self.enabled_var.set(bool(route.enabled))
        self.name_var.set(route.name)
        self.group_var.set(route.message_group)
        self.title_var.set(route.title)
        self.body_format_var.set(
            BODY_FORMAT_LABELS.get(route.body_format, BODY_FORMAT_LABELS[DEFAULT_BODY_FORMAT])
        )
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", route.body)
        self.url_var.set(route.url)
        self.method_var.set(route.method)
        self.headers_text.delete("1.0", "end")
        self.headers_text.insert("1.0", format_header_lines(route.headers))
        self._update_body_hint()

    def _save(self) -> None:
        try:
            headers = parse_header_lines(self.headers_text.get("1.0", "end"))
            self.result = WebhookRoute.from_dict(
                {
                    "id": self.route.id if self.route else "",
                    "name": self.name_var.get(),
                    "enabled": bool(self.enabled_var.get()),
                    "message_group": self.group_var.get(),
                    "title": self.title_var.get(),
                    "body_format": self._body_format(),
                    "body": self.body_text.get("1.0", "end").rstrip("\n"),
                    "url": self.url_var.get(),
                    "method": self.method_var.get(),
                    "headers": headers,
                }
            )
        except ValueError as exc:
            messagebox.showerror("Invalid routing rule", str(exc), parent=self.window)
            return
        self.close()

    def show(self) -> None:
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        try:
            self.window.attributes("-topmost", True)
            self.window.attributes("-topmost", False)
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        try:
            self.window.destroy()
        except Exception:  # pragma: no cover
            pass
