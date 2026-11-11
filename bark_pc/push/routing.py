"""Incoming-message routing rules → webhooks.

The user can describe rules such as "messages in group `ops` whose title
contains `prod` go to this Slack webhook".  Rules are deliberately simple:

* **message group** matches the push's ``group`` field (case-insensitive).  For
  Supabase rooms, the room name becomes the group.  An empty value matches
  every message.
* **title** is a case-insensitive substring of the displayed title (``title``
  plus ``subtitle``).  An empty value matches every message.
* a rule fires when *all* of its non-empty conditions match, and the matching
  notification is POSTed (or PUT/PATCHed) to the webhook URL as JSON.

Delivery happens on a small daemon worker so a slow webhook can never delay
ingest or a toast.  Failures are logged and dropped: routing is best-effort and
must never affect local delivery.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from ..core.config import Paths, atomic_write_text
from ..core.http_debug import client_event_hooks
from ..core.models import Notification

logger = logging.getLogger("bark_pc.routing")

MAX_ROUTES = 100
MAX_ROUTE_NAME_LENGTH = 80
MAX_ROUTE_MATCH_LENGTH = 200
MAX_WEBHOOK_URL_LENGTH = 2048
MAX_HEADER_COUNT = 30
MAX_HEADER_NAME_LENGTH = 80
MAX_HEADER_VALUE_LENGTH = 2048
WEBHOOK_METHODS = ("GET", "POST", "PUT", "PATCH")
DEFAULT_WEBHOOK_METHOD = "GET"
CONTENT_MODE_NONE = "none"
CONTENT_MODE_TITLE = "title"
CONTENT_MODE_BODY = "body"
CONTENT_MODE_FULL = "full"
CONTENT_MODES = (
    CONTENT_MODE_NONE,
    CONTENT_MODE_TITLE,
    CONTENT_MODE_BODY,
    CONTENT_MODE_FULL,
)
DEFAULT_CONTENT_MODE = CONTENT_MODE_NONE
CONTENT_MODE_LABELS = {
    CONTENT_MODE_NONE: "No message content",
    CONTENT_MODE_TITLE: "Title only",
    CONTENT_MODE_BODY: "Title and body",
    CONTENT_MODE_FULL: "Title, body and raw payload",
}
CONTENT_LABEL_TO_MODE = {
    label: mode for mode, label in CONTENT_MODE_LABELS.items()
}
WEBHOOK_TIMEOUT_S = 10.0
WEBHOOK_QUEUE_SIZE = 256
URL_PLACEHOLDERS = (
    "id",
    "remote_id",
    "group",
    "title",
    "subtitle",
    "display_title",
    "body",
    "url",
    "image_url",
    "source",
    "received_at",
    "received_at_iso",
)
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")

_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
@dataclass
class WebhookRoute:
    """One "match these messages → call this webhook" rule."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    enabled: bool = True
    message_group: str = ""
    title: str = ""
    url: str = ""
    method: str = DEFAULT_WEBHOOK_METHOD
    content: str = DEFAULT_CONTENT_MODE
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> WebhookRoute:
        """Build a validated rule or raise ``ValueError`` explaining the problem."""
        if not isinstance(data, dict):
            raise ValueError("route must be an object")
        raw_id = str(data.get("id") or "").strip()
        raw_name = str(data.get("name") or "").strip()
        if len(raw_name) > MAX_ROUTE_NAME_LENGTH:
            raise ValueError(f"name must be at most {MAX_ROUTE_NAME_LENGTH} characters")
        raw_enabled = data.get("enabled", True)
        if not isinstance(raw_enabled, bool):
            raise ValueError("enabled must be true or false")
        raw_group = str(
            data.get("message_group")
            or data.get("message_type")
            or data.get("type")
            or ""
        ).strip()
        raw_title = str(data.get("title") or "").strip()
        for label, value in (("message group", raw_group), ("title", raw_title)):
            if len(value) > MAX_ROUTE_MATCH_LENGTH:
                raise ValueError(
                    f"{label} must be at most {MAX_ROUTE_MATCH_LENGTH} characters"
                )
        raw_method = str(data.get("method") or DEFAULT_WEBHOOK_METHOD).strip().upper()
        if raw_method not in WEBHOOK_METHODS:
            raise ValueError("method must be " + ", ".join(WEBHOOK_METHODS))
        raw_content = str(data.get("content") or DEFAULT_CONTENT_MODE).strip().lower()
        if raw_content not in CONTENT_MODES:
            raise ValueError("content must be " + ", ".join(CONTENT_MODES))
        url = validate_webhook_url(str(data.get("url") or ""))
        validate_webhook_placeholders(url)
        return cls(
            id=raw_id or uuid.uuid4().hex,
            name=raw_name,
            enabled=raw_enabled,
            message_group=raw_group,
            title=raw_title,
            url=url,
            method=raw_method,
            content=raw_content,
            headers=validate_headers(data.get("headers") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "message_group": self.message_group,
            "title": self.title,
            "url": self.url,
            "method": self.method,
            "content": self.content,
            "headers": dict(self.headers),
        }

    @property
    def display_name(self) -> str:
        return (self.name or "").strip() or self.url

    @property
    def host(self) -> str:
        try:
            return urlsplit(self.url).netloc or self.url
        except ValueError:  # pragma: no cover - validate_webhook_url prevents this
            return self.url

    @property
    def match_summary(self) -> str:
        parts = []
        if self.message_group:
            parts.append(f"group {self.message_group}")
        if self.title:
            parts.append(f'title contains "{self.title}"')
        return " · ".join(parts) or "every message"


def validate_webhook_url(raw: str) -> str:
    """Canonicalize a webhook URL (http/https, no credentials, no fragment)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("webhook URL is empty")
    if len(text) > MAX_WEBHOOK_URL_LENGTH:
        raise ValueError(f"webhook URL must be at most {MAX_WEBHOOK_URL_LENGTH} characters")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("webhook URL must use http:// or https://")
    if not parts.hostname:
        raise ValueError("webhook URL has no host")
    if parts.username or parts.password:
        raise ValueError("webhook URL must not contain credentials")
    if parts.fragment:
        raise ValueError("webhook URL must not contain a fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"webhook URL has an invalid port: {exc}") from exc
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port:
        netloc = f"{netloc}:{port}"
    # Keep the path exactly as typed: many webhook services are suffix-sensitive.
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def validate_headers(raw) -> dict[str, str]:
    """Validate a header mapping so it can be handed to httpx."""
    if raw in (None, ""):
        return {}
    if not isinstance(raw, dict):
        raise ValueError("headers must be an object")
    if len(raw) > MAX_HEADER_COUNT:
        raise ValueError(f"at most {MAX_HEADER_COUNT} headers are allowed")
    headers: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        text = str(value if value is not None else "").strip()
        if not name:
            raise ValueError("a header name cannot be empty")
        if len(name) > MAX_HEADER_NAME_LENGTH:
            raise ValueError(
                f"header names must be at most {MAX_HEADER_NAME_LENGTH} characters"
            )
        if not _HEADER_NAME.match(name):
            raise ValueError(f"invalid header name: {name!r}")
        if len(text) > MAX_HEADER_VALUE_LENGTH:
            raise ValueError(
                f"header values must be at most {MAX_HEADER_VALUE_LENGTH} characters"
            )
        if "\r" in text or "\n" in text:
            raise ValueError(f"header {name!r} must not contain a line break")
        headers[name] = text
    return headers


def parse_header_lines(text: str) -> dict[str, str]:
    """Parse ``Name: value`` lines from the dialog into a header mapping."""
    headers: dict[str, str] = {}
    for number, line in enumerate((text or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            raise ValueError(f"header line {number} must look like Name: value")
        headers[name.strip()] = value.strip()
    return validate_headers(headers)


def format_header_lines(headers: dict[str, str]) -> str:
    return "\n".join(f"{name}: {value}" for name, value in (headers or {}).items())


class RouteStore:
    """Load/save ``routes.json`` next to ``settings.json``."""

    def __init__(self, paths: Paths, logger_: logging.Logger | None = None):
        self.paths = paths
        self.logger = logger_ or logger
        self._routes: list[WebhookRoute] | None = None

    @property
    def path(self) -> Path:
        return self.paths.routes_path

    @property
    def routes(self) -> list[WebhookRoute]:
        if self._routes is None:
            self._routes = self.load()
        return self._routes

    def load(self) -> list[WebhookRoute]:
        path = self.path
        if not path.exists():
            self._routes = []
            return self._routes
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.logger.warning("routes: cannot read %s: %s; using none", path, exc)
            self._routes = []
            return self._routes
        items = data.get("routes") if isinstance(data, dict) else data
        if not isinstance(items, list):
            self.logger.warning("routes: %s does not contain a list; using none", path)
            self._routes = []
            return self._routes
        routes: list[WebhookRoute] = []
        for index, item in enumerate(items, start=1):
            if len(routes) >= MAX_ROUTES:
                self.logger.warning("routes: ignoring entries after the first %s", MAX_ROUTES)
                break
            try:
                routes.append(WebhookRoute.from_dict(item))
            except ValueError as exc:
                self.logger.warning("routes: ignoring entry %s (%s)", index, exc)
        self._routes = routes
        return routes

    def save(self, routes: list[WebhookRoute]) -> list[WebhookRoute]:
        validated: list[WebhookRoute] = []
        for index, route in enumerate(routes[:MAX_ROUTES], start=1):
            try:
                validated.append(WebhookRoute.from_dict(route.to_dict()))
            except ValueError as exc:
                raise ValueError(f"route {index}: {exc}") from exc
        payload = {"version": 1, "routes": [route.to_dict() for route in validated]}
        atomic_write_text(
            self.path, json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        self._routes = validated
        return validated


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def _raw_payload(notification: Notification) -> dict:
    try:
        parsed = json.loads(notification.raw_json or "")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def notification_group(notification: Notification) -> str:
    """The group a rule matches; empty when the push carried no group."""
    return str(notification.group_name or "").strip()


def route_matches(route: WebhookRoute, notification: Notification) -> bool:
    if not route.enabled:
        return False
    if route.message_group:
        wanted = route.message_group.casefold()
        if wanted != notification_group(notification).casefold():
            return False
    if route.title:
        wanted = route.title.casefold()
        haystack = (notification.display_title or "").casefold()
        if wanted not in haystack:
            return False
    return True


def matching_routes(routes, notification: Notification) -> list[WebhookRoute]:
    return [route for route in routes if route_matches(route, notification)]


# --------------------------------------------------------------------------- #
# Webhook request body
# --------------------------------------------------------------------------- #
def _received_at_iso(timestamp_ms: int | None) -> str | None:
    if not timestamp_ms:
        return None
    try:
        moment = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat()


def webhook_payload(notification: Notification, route: WebhookRoute) -> dict:
    """The JSON document sent to POST/PUT/PATCH webhooks.

    Message text is private by default: only metadata is sent unless the route
    opts into a content mode.  ``title``/``subtitle`` arrive with the *title*
    mode, the body with *body*, and the original custom payload with *full*.
    """
    raw = _raw_payload(notification)
    payload = {
        "event": "bark.notification",
        "route": {"id": route.id, "name": route.name},
        "id": notification.id,
        "remote_id": notification.remote_id,
        "group": notification.group_name,
        "url": notification.url,
        "image_url": notification.image_url,
        "source": notification.source_server,
        "received_at": notification.received_at,
        "received_at_iso": _received_at_iso(notification.received_at),
    }
    if route.content in (CONTENT_MODE_TITLE, CONTENT_MODE_BODY, CONTENT_MODE_FULL):
        payload.update(
            title=notification.title,
            subtitle=notification.subtitle,
            display_title=notification.display_title,
        )
    if route.content in (CONTENT_MODE_BODY, CONTENT_MODE_FULL):
        payload["body"] = notification.body
    if route.content == CONTENT_MODE_FULL:
        payload["payload"] = raw
    return payload


def validate_webhook_placeholders(url: str) -> None:
    """Reject unknown ``{placeholder}`` names in a webhook URL."""
    for name in _PLACEHOLDER.findall(url or ""):
        if name.casefold() not in URL_PLACEHOLDERS:
            supported = ", ".join("{" + item + "}" for item in URL_PLACEHOLDERS)
            raise ValueError(
                f"unknown URL placeholder {{{name}}}; supported placeholders: {supported}"
            )


def format_webhook_url(notification: Notification, route: WebhookRoute) -> str:
    """Fill ``{title}``-style placeholders in the configured webhook URL.

    No fields are appended automatically.  A placeholder is the explicit way to
    put message data into the URL, and its value is URL-encoded so it cannot
    change the URL structure.  Missing values become an empty string.
    """
    values = {
        "id": notification.id,
        "remote_id": notification.remote_id,
        "group": notification.group_name,
        "title": notification.title,
        "subtitle": notification.subtitle,
        "display_title": notification.display_title,
        "body": notification.body,
        "url": notification.url,
        "image_url": notification.image_url,
        "source": notification.source_server,
        "received_at": notification.received_at,
        "received_at_iso": _received_at_iso(notification.received_at),
    }

    def replace(match: re.Match) -> str:
        value = values.get(match.group(1).casefold())
        return quote("" if value is None else str(value), safe="")

    return _PLACEHOLDER.sub(replace, route.url)


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
class WebhookDispatcher:
    """Deliver matching notifications on one small background thread."""

    def __init__(
        self,
        routes=(),
        *,
        logger_: logging.Logger | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = WEBHOOK_TIMEOUT_S,
    ):
        self.logger = logger_ or logger
        self.timeout = timeout
        self._routes: list[WebhookRoute] = list(routes)
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=WEBHOOK_QUEUE_SIZE)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            follow_redirects=False,
            event_hooks=client_event_hooks(),
        )

    # -- state ------------------------------------------------------------- #
    @property
    def routes(self) -> list[WebhookRoute]:
        with self._lock:
            return list(self._routes)

    def set_routes(self, routes) -> None:
        with self._lock:
            self._routes = list(routes)
        self.logger.info("routing: %s webhook rule(s) active", len(self._routes))

    def submit(self, notification: Notification) -> int:
        """Queue *notification* for every matching rule; returns the count."""
        if notification is None or notification.from_self:
            return 0
        with self._lock:
            matches = [route for route in self._routes if route_matches(route, notification)]
        if not matches:
            return 0
        self._ensure_thread()
        queued = 0
        for route in matches:
            try:
                self._queue.put_nowait((route, notification))
                queued += 1
            except queue.Full:
                self.logger.warning(
                    "routing: queue is full; dropping webhook %s", route.host
                )
                break
        return queued

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="bark-webhooks", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            route, notification = item
            try:
                self.deliver(route, notification)
            except Exception:  # pragma: no cover - the worker must never die
                self.logger.exception("routing: webhook delivery failed")

    def deliver(
        self,
        route: WebhookRoute,
        notification: Notification,
        *,
        client: httpx.Client | None = None,
    ) -> httpx.Response | None:
        """Send one webhook request; never raises for network/HTTP failures."""
        request_client = client or self._client
        try:
            url = format_webhook_url(notification, route)
            if route.method == "GET":
                response = request_client.get(
                    url,
                    headers=dict(route.headers),
                )
            else:
                response = request_client.request(
                    route.method,
                    url,
                    json=webhook_payload(notification, route),
                    headers=dict(route.headers),
                )
        except httpx.TimeoutException as exc:
            self.logger.warning("routing: webhook %s timed out: %s", route.host, exc)
            return None
        except httpx.HTTPError as exc:
            self.logger.warning("routing: webhook %s failed: %s", route.host, exc)
            return None
        except Exception as exc:  # pragma: no cover - defensive
            self.logger.warning("routing: webhook %s failed: %s", route.host, exc)
            return None
        if response.status_code >= 400:
            detail = (response.text or "").strip().replace("\n", " ")[:200]
            self.logger.warning(
                "routing: webhook %s returned HTTP %s%s",
                route.host,
                response.status_code,
                f" ({detail})" if detail else "",
            )
        else:
            self.logger.debug(
                "routing: webhook %s accepted a message (HTTP %s)",
                route.host,
                response.status_code,
            )
        return response

    def close(self, timeout: float = 1.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:  # pragma: no cover - worker is busy but will see _stop
            pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        try:
            self._client.close()
        except Exception:  # pragma: no cover - defensive
            pass
