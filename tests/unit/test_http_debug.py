"""HTTP debug logging: request/response hooks, URL masking, library bridging."""

from __future__ import annotations

import logging

import anyio
import httpx
import pytest

from bark_pc.bark.server_client import BarkServerClient
from bark_pc.core.http_debug import (
    MASKED,
    alog_request,
    alog_response,
    async_client_event_hooks,
    client_event_hooks,
    log_request,
    log_response,
    safe_url,
)
from bark_pc.core.logging import BRIDGED_LOGGERS, build_logger
from bark_pc.images.fetcher import ImageFetcher

HTTP = "bark_pc.http"


@pytest.fixture(autouse=True)
def restore_bridged_loggers():
    """`build_logger` mutates the httpx/httpcore loggers; put them back."""
    yield
    for name in BRIDGED_LOGGERS:
        library = logging.getLogger(name)
        library.handlers = []
        library.setLevel(logging.NOTSET)
        library.propagate = True


# --------------------------------------------------------------------------- #
# URL masking
# --------------------------------------------------------------------------- #


def test_safe_url_masks_credential_query_values():
    url = httpx.URL("https://api.day.app/push?token=secretvalue&device_key=abc&x=1")

    masked = safe_url(url)

    assert masked.startswith("https://api.day.app/push?")
    assert "secretvalue" not in masked
    assert "device_key=abc" not in masked
    assert f"token={MASKED}" in masked
    assert masked.endswith("x=1")


def test_safe_url_leaves_plain_urls_untouched():
    url = httpx.URL("https://api.day.app/ping")

    assert safe_url(url) == "https://api.day.app/ping"


# --------------------------------------------------------------------------- #
# Sync hooks
# --------------------------------------------------------------------------- #


def client_with_hooks(handler=None):
    handler = handler or (lambda request: httpx.Response(200, json={"ok": True}))
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        event_hooks=client_event_hooks(),
    )


def test_sync_hooks_log_the_request_and_the_response(caplog):
    with caplog.at_level(logging.DEBUG, logger=HTTP):
        with client_with_hooks() as client:
            client.get("https://server.test/ping")

    assert "http: -> GET https://server.test/ping" in caplog.text
    response_line = next(
        record.getMessage() for record in caplog.records if "http: <-" in record.getMessage()
    )
    assert "http: <- 200 GET https://server.test/ping" in response_line
    assert "in " in response_line and "ms" in response_line


def test_sync_hooks_are_silent_above_debug_level(caplog):
    with caplog.at_level(logging.INFO, logger=HTTP):
        with client_with_hooks() as client:
            client.get("https://server.test/ping")

    assert "http:" not in caplog.text


def test_sync_hooks_never_log_credentials(caplog):
    with caplog.at_level(logging.DEBUG, logger=HTTP):
        with client_with_hooks() as client:
            client.get("https://server.test/push?token=supersecret&device_key=abc123")

    assert "supersecret" not in caplog.text
    assert "device_key=abc123" not in caplog.text
    assert f"token={MASKED}" in caplog.text


def test_hooks_record_the_content_length_header(caplog):
    with caplog.at_level(logging.DEBUG, logger=HTTP):
        request = httpx.Request("GET", "https://server.test/image.png")
        response = httpx.Response(200, headers={"content-length": "1234"}, request=request)
        anyio.run(alog_request, request)
        anyio.run(alog_response, response)

    assert "1234 bytes" in caplog.text


# --------------------------------------------------------------------------- #
# Async hooks
# --------------------------------------------------------------------------- #


def test_async_hooks_log_the_request_and_the_response(caplog):
    async def scenario():
        transport = httpx.MockTransport(lambda request: httpx.Response(204))
        async with httpx.AsyncClient(
            transport=transport, event_hooks=async_client_event_hooks()
        ) as client:
            await client.delete("https://server.test/register/abc")

    with caplog.at_level(logging.DEBUG, logger=HTTP):
        anyio.run(scenario)

    assert "http: -> DELETE https://server.test/register/abc" in caplog.text
    assert "http: <- 204 DELETE https://server.test/register/abc" in caplog.text


def test_async_response_hook_survives_an_unread_body(caplog):
    request = httpx.Request("GET", "https://server.test/x")
    response = httpx.Response(200, stream=httpx.ByteStream(b"payload"), request=request)

    with caplog.at_level(logging.DEBUG, logger=HTTP):
        anyio.run(alog_request, request)
        anyio.run(alog_response, response)

    assert "http: <- 200 GET https://server.test/x" in caplog.text
    # The hook must not consume the body for the caller.
    assert response.read() == b"payload"


# --------------------------------------------------------------------------- #
# Client wiring
# --------------------------------------------------------------------------- #


def test_server_client_attaches_the_debug_hooks():
    client = BarkServerClient("https://server.test")

    assert [hook.__name__ for hook in client._client.event_hooks["request"]] == ["log_request"]
    assert [hook.__name__ for hook in client._client.event_hooks["response"]] == ["log_response"]
    client.close()


def test_server_client_leaves_an_injected_client_alone():
    injected = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))

    client = BarkServerClient("https://server.test", client=injected)

    assert injected.event_hooks["request"] == []
    client.close()


class _Store:
    def get(self, url):  # pragma: no cover - not used by these tests
        return None


def test_image_fetcher_attaches_the_debug_hooks():
    fetcher = ImageFetcher(_Store())  # type: ignore[arg-type]

    assert fetcher._client.event_hooks["request"] == [log_request]
    assert fetcher._client.event_hooks["response"] == [log_response]
    fetcher._client.close()


def test_activation_client_uses_the_shared_debug_hooks():
    from bark_pc.push import pypush_client

    hooks = pypush_client._activation_event_hooks()

    assert alog_request in hooks["request"]
    assert alog_response in hooks["response"]
    assert pypush_client._log_activation_response in hooks["response"]


# --------------------------------------------------------------------------- #
# Library bridging
# --------------------------------------------------------------------------- #


def read_log(path) -> str:
    return path.read_text(encoding="utf-8")


def test_debug_level_bridges_httpx_and_httpcore_into_the_app_log(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")

    logging.getLogger("httpx").info('HTTP Request: GET https://server.test/ping "200 OK"')
    logging.getLogger("httpcore.connection").debug("connect_tcp.started host='server.test'")

    content = read_log(log_path)
    assert "HTTP Request: GET https://server.test/ping" in content
    assert "connect_tcp.started" in content


def test_info_level_does_not_bridge_library_loggers(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="info")

    logging.getLogger("httpx").info("HTTP Request: GET https://server.test/ping")

    assert logging.getLogger("httpx").level == logging.NOTSET
    assert "HTTP Request" not in read_log(log_path)


def test_bridging_keeps_redaction_in_place(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")

    logging.getLogger("httpx").info(
        "HTTP Request: GET https://server.test/push?token=supersecret&device_key=abc 200 OK"
    )

    content = read_log(log_path)
    assert "supersecret" not in content
    # The handler's redaction filter masks it even though the message came from httpx.
    assert "token=[redacted]" in content


def test_rebuilding_the_logger_does_not_duplicate_bridge_handlers(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")
    build_logger(log_path, level="debug")

    https = logging.getLogger("httpx")
    assert len([h for h in https.handlers if getattr(h, "_bark_library_bridge", False)]) == 1

    https.info("HTTP Request: GET https://server.test/once")
    assert read_log(log_path).count("server.test/once") == 1


def test_switching_back_to_info_detaches_the_bridge(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")
    build_logger(log_path, level="info")

    https = logging.getLogger("httpx")
    assert https.handlers == []
    assert https.propagate is True
    assert https.level == logging.NOTSET
