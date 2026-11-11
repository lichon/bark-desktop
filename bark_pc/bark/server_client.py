"""Minimal Bark-compatible server client (register / verify / ping / push).

Uses `httpx` with an injectable transport so the unit tests never open a socket.
Every failure is mapped onto `core.errors` so the UI can present a sentence
instead of an HTTP status.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlsplit

import httpx

from ..core.config import validate_bark_push_url, validate_server_url
from ..core.errors import (
    BarkError,
    RegistrationRejected,
    ServerError,
    ServerRejected,
    ServerTimeout,
    ServerUnreachable,
)
from ..core.http_debug import client_event_hooks

logger = logging.getLogger("bark_pc.bark.server")

DEFAULT_TIMEOUT = 10.0
MAX_TOKEN_LENGTH = 160
MAX_KEY_LENGTH = 160


def normalize_server_url(raw: str) -> str:
    """Normalize `host`, `http://host`, `https://host/prefix/` → canonical form."""
    try:
        return validate_server_url(raw)
    except ValueError as exc:
        raise BarkError(f"Invalid server address: {exc}") from exc


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:  # pragma: no cover - defensive
        return url


class BarkServerClient:
    """One client per (server URL, operation); cheap and thread-local."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
    ):
        self.raw_url = base_url
        self.base_url = normalize_server_url(base_url)
        self.host = _host_of(self.base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=False,
            event_hooks=client_event_hooks(),
        )
        self.timeout = timeout

    def close(self) -> None:
        if self._owns_client:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> BarkServerClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- requests ---------------------------------------------------------- #
    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.request(method, url, json=json_body)
        except httpx.TimeoutException as exc:
            raise ServerTimeout(detail=str(exc)) from exc
        except httpx.TransportError as exc:
            raise ServerUnreachable(self.host, detail=str(exc)) from exc

        status = response.status_code
        payload: dict = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            parsed = None

        if status in (401, 403):
            raise ServerRejected(status)
        if status >= 500:
            raise ServerError(payload.get("message") or f"HTTP {status}")
        if status >= 400:
            message = str(payload.get("message") or f"HTTP {status}")
            raise RegistrationRejected.from_server(message)
        if status != 200:
            raise ServerError(f"unexpected HTTP {status}")
        if parsed is None:
            raise ServerError("the server returned a non-JSON response")
        code = payload.get("code")
        if isinstance(code, int) and code != 200:
            message = str(payload.get("message") or f"code {code}")
            if code in (401, 403):
                raise ServerRejected(code)
            raise RegistrationRejected.from_server(message)
        return payload

    # -- API --------------------------------------------------------------- #
    def register(
        self,
        device_token: str,
        device_key: str | None = None,
        *,
        device_type: str = "ios",
    ) -> str:
        token = (device_token or "").strip()
        if not token:
            raise RegistrationRejected("device token is empty")
        if len(token) > MAX_TOKEN_LENGTH:
            raise RegistrationRejected(
                f"device token is invalid (longer than {MAX_TOKEN_LENGTH} characters)"
            )
        if device_type not in ("ios", "watchos"):
            raise RegistrationRejected(f"unsupported device type: {device_type}")

        body: dict = {"device_token": token, "device_type": device_type}
        if device_key:
            body["device_key"] = device_key

        payload = self._request("POST", "/register", json_body=body)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RegistrationRejected("the server did not return registration data")
        key = data.get("device_key") or data.get("key")
        if not key or not isinstance(key, str):
            raise RegistrationRejected("the server did not return a device key")
        logger.info("registered a device key (%s…)", key[:4])
        return key

    def verify_key(self, device_key: str) -> bool:
        if not device_key:
            return False
        try:
            self._request("GET", f"/register/{device_key}")
        except RegistrationRejected:
            return False
        except ServerRejected as exc:
            if exc.status == 403:
                return False
            raise
        return True

    def ping(self) -> bool:
        url = f"{self.base_url}/ping"
        try:
            response = self._client.request("GET", url)
        except httpx.TimeoutException as exc:
            raise ServerTimeout(detail=str(exc)) from exc
        except httpx.TransportError as exc:
            raise ServerUnreachable(self.host, detail=str(exc)) from exc
        if response.status_code in (401, 403):
            raise ServerRejected(
                response.status_code,
                "The server requires authentication, which is not supported.",
            )
        if response.status_code != 200:
            raise ServerUnreachable(
                self.host, f"Cannot reach {self.host} (HTTP {response.status_code})."
            )
        return True

    def push_test(
        self,
        device_key: str,
        *,
        title: str = "Bark Desktop",
        body: str = "Test notification",
        group: str = "bark-desktop",
        message_id: str | None = None,
    ) -> dict:
        if not device_key:
            raise RegistrationRejected("no device key to push to")
        payload = self._request(
            "POST",
            "/push",
            json_body={
                "device_key": device_key,
                "title": title,
                "body": body,
                "group": group,
                "id": message_id or str(uuid.uuid4()),
            },
        )
        return payload


def push_to_url(
    push_url: str,
    *,
    title: str = "",
    body: str = "",
    group: str | None = None,
    message_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """POST a message to a Bark push URL that already carries a device key.

    This is the route used by the history window's compose bar when the user
    chooses *Bark URL*.  Bark accepts `POST https://server/<device_key>` with a
    JSON body, so the configured URL is used verbatim (query parameters such as
    `group=` are left intact).  Failures map onto the same `core.errors`
    taxonomy as the rest of the Bark client.
    """
    url = validate_bark_push_url(push_url)
    if not url:
        raise RegistrationRejected("no Bark URL configured for self messages")
    host = _host_of(url)
    payload: dict = {
        "title": title or "",
        "body": body or "",
        "id": message_id or uuid.uuid4().hex,
    }
    if group:
        payload["group"] = group
    try:
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=False,
            event_hooks=client_event_hooks(),
        ) as client:
            response = client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        raise ServerTimeout(detail=str(exc)) from exc
    except httpx.TransportError as exc:
        raise ServerUnreachable(host, detail=str(exc)) from exc

    status = response.status_code
    parsed: dict | None = None
    try:
        candidate = response.json()
        if isinstance(candidate, dict):
            parsed = candidate
    except ValueError:
        parsed = None
    if status in (401, 403):
        raise ServerRejected(status, "The Bark URL rejected the message (401/403).")
    if status >= 500:
        message = (parsed or {}).get("message") or f"HTTP {status}"
        raise ServerError(message)
    if status >= 400:
        message = str((parsed or {}).get("message") or f"HTTP {status}")
        raise RegistrationRejected.from_server(message)
    if status != 200:
        raise ServerError(f"unexpected HTTP {status}")
    if parsed is None:
        raise ServerError("the Bark URL returned a non-JSON response")
    code = parsed.get("code")
    if isinstance(code, int) and code != 200:
        message = str(parsed.get("message") or f"code {code}")
        if code in (401, 403):
            raise ServerRejected(code, message)
        raise RegistrationRejected.from_server(message)
    return parsed
