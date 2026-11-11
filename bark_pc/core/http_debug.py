"""Debug logging for every outbound HTTP request.

Clients built by this app attach the hooks below, so each request is logged as
`-> METHOD url` and each response as `<- METHOD url status (bytes, ms)` at DEBUG
level.  `core.logging.build_logger` additionally bridges the `httpx` and
`httpcore` loggers into the app log, which covers clients this app does not
build itself (for example pypush's activation client).

Values that commonly carry credentials are masked here; everything else is
still passed through the redaction filter that every handler installs.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx

logger = logging.getLogger("bark_pc.http")

# Query parameters whose values are never written to the log.
SENSITIVE_PARAMS = frozenset(
    {
        "token",
        "key",
        "device_key",
        "device_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "password",
        "sig",
        "signature",
    }
)

_STARTED_AT = "bark_pc_started_at"
# Kept free of brackets so `urlencode` leaves it readable.
MASKED = "redacted"


def safe_url(url: httpx.URL) -> str:
    """Return *url* with sensitive query values masked."""
    params = url.params.multi_items()
    if not params:
        return str(url)
    masked = urlencode(
        [(name, MASKED if name.lower() in SENSITIVE_PARAMS else value) for name, value in params]
    )
    return f"{url.copy_with(query=None)}?{masked}"


def _start(request: httpx.Request) -> None:
    request.extensions[_STARTED_AT] = time.perf_counter()
    logger.debug("http: -> %s %s", request.method, safe_url(request.url))


def _describe(response: httpx.Response) -> str:
    request = response.request
    started = request.extensions.get(_STARTED_AT)
    elapsed = f" in {(time.perf_counter() - started) * 1000:.0f} ms" if started else ""
    length = response.headers.get("content-length")
    size = f", {length} bytes" if length is not None else ""
    return (
        f"http: <- {response.status_code} {request.method} {safe_url(request.url)}{elapsed}{size}"
    )


def log_request(request: httpx.Request) -> None:
    _start(request)


def log_response(response: httpx.Response) -> None:
    logger.debug(_describe(response))


async def alog_request(request: httpx.Request) -> None:
    _start(request)


async def alog_response(response: httpx.Response) -> None:
    logger.debug(_describe(response))


def client_event_hooks() -> dict[str, list]:
    """Hooks for `httpx.Client`."""
    return {"request": [log_request], "response": [log_response]}


def async_client_event_hooks() -> dict[str, list]:
    """Hooks for `httpx.AsyncClient`."""
    return {"request": [alog_request], "response": [alog_response]}
