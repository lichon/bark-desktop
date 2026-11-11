"""Bark server client request/response shapes via `httpx.MockTransport`."""

from __future__ import annotations

import json

import httpx
import pytest

from bark_pc.bark.server_client import (
    BarkServerClient,
    normalize_server_url,
    push_to_url,
)
from bark_pc.core.errors import (
    BarkError,
    RegistrationRejected,
    ServerError,
    ServerRejected,
    ServerTimeout,
    ServerUnreachable,
)


def client_with(handler, base: str = "http://127.0.0.1:8080") -> BarkServerClient:
    return BarkServerClient(base, transport=httpx.MockTransport(handler))


def test_register_success_reads_device_key_and_sends_the_expected_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            200,
            json={"code": 200, "data": {"device_key": "abc123KEY", "key": "legacy"}},
        )

    client = client_with(handler)
    key = client.register("ab" * 32)
    assert key == "abc123KEY"
    assert captured["method"] == "POST"
    assert captured["path"] == "/register"
    assert captured["body"] == {"device_token": "ab" * 32, "device_type": "ios"}
    assert "application/json" in captured["content_type"]


def test_register_falls_back_to_the_legacy_key_field():
    client = client_with(
        lambda request: httpx.Response(200, json={"code": 200, "data": {"key": "legacyKey"}})
    )
    assert client.register("ab" * 32) == "legacyKey"


def test_register_includes_an_existing_device_key_when_rotating():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"device_key": "same"}})

    client_with(handler).register("ab" * 32, device_key="existing-key")
    assert captured["body"]["device_key"] == "existing-key"


@pytest.mark.parametrize("token", ["", "   ", None])
def test_empty_token_is_rejected_client_side(token):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200, json={})

    client = client_with(handler)
    with pytest.raises(RegistrationRejected):
        client.register(token)
    assert calls == []


def test_long_token_is_rejected_before_the_request():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        calls.append(request)
        return httpx.Response(200, json={})

    client = client_with(handler)
    with pytest.raises(RegistrationRejected) as excinfo:
        client.register("a" * 161)
    assert "160" in str(excinfo.value)
    assert calls == []


def test_closed_registration_maps_to_server_rejected():
    client = client_with(lambda request: httpx.Response(403, json={"code": 403}))
    with pytest.raises(ServerRejected) as excinfo:
        client.register("ab" * 32)
    assert excinfo.value.status == 403
    assert "403" in str(excinfo.value)


def test_500_maps_to_server_error():
    client = client_with(lambda request: httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(ServerError):
        client.register("ab" * 32)


def test_400_message_is_surfaced_verbatim():
    client = client_with(
        lambda request: httpx.Response(
            400, json={"code": 400, "message": "device token is invalid"}
        )
    )
    with pytest.raises(RegistrationRejected) as excinfo:
        client.register("ab" * 32)
    assert "device token is invalid" in str(excinfo.value)


def test_200_with_error_code_is_rejected():
    client = client_with(
        lambda request: httpx.Response(200, json={"code": 400, "message": "bad key"})
    )
    with pytest.raises(RegistrationRejected):
        client.register("ab" * 32)


def test_malformed_success_body_is_rejected():
    client = client_with(lambda request: httpx.Response(200, json={"code": 200}))
    with pytest.raises(RegistrationRejected):
        client.register("ab" * 32)


def test_verify_key_success_and_unknown_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/register/known"
        return httpx.Response(200, json={"code": 200, "message": "success"})

    assert client_with(handler).verify_key("known") is True

    def unknown(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 400, "message": "failed to get device token"})

    assert client_with(unknown).verify_key("missing") is False


def test_ping_success_and_failure():
    assert client_with(lambda request: httpx.Response(200, text="pong")).ping() is True

    with pytest.raises(ServerUnreachable):
        client_with(lambda request: httpx.Response(502, text="bad gateway")).ping()


def test_ping_401_reports_authentication_is_unsupported():
    with pytest.raises(ServerRejected) as excinfo:
        client_with(lambda request: httpx.Response(401)).ping()
    assert "authentication" in str(excinfo.value).lower()


def test_push_test_posts_the_expected_fields():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200})

    payload = client_with(handler).push_test("KEY", message_id="fixed-id")
    assert payload["code"] == 200
    assert captured["path"] == "/push"
    assert captured["body"]["device_key"] == "KEY"
    assert captured["body"]["id"] == "fixed-id"
    assert captured["body"]["title"] == "Bark Desktop"


def test_push_failure_maps_to_server_error():
    client = client_with(lambda request: httpx.Response(500, json={"message": "no devices"}))
    with pytest.raises(ServerError):
        client.push_test("KEY")


def test_transport_error_maps_to_server_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(ServerUnreachable) as excinfo:
        client_with(handler).register("ab" * 32)
    assert "127.0.0.1" in str(excinfo.value)


def test_timeout_maps_to_server_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ServerTimeout):
        client_with(handler).register("ab" * 32)


def test_normalize_rejects_unsupported_schemes():
    with pytest.raises(BarkError):
        normalize_server_url("ftp://example.com")
    assert normalize_server_url("example.com/") == "https://example.com"
    assert normalize_server_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"


def test_push_to_url_posts_json_and_preserves_query_parameters():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 200, "message": "success"})

    push_to_url(
        "http://127.0.0.1:8080/deviceKEY/?group=ops",
        title="WORKSTATION",
        body="hello from the PC",
        group="bark-desktop",
        message_id="fixed-id",
        transport=httpx.MockTransport(handler),
    )

    assert captured["url"] == "http://127.0.0.1:8080/deviceKEY?group=ops"
    assert captured["body"] == {
        "title": "WORKSTATION",
        "body": "hello from the PC",
        "group": "bark-desktop",
        "id": "fixed-id",
    }


def test_push_to_url_requires_a_configured_url():
    with pytest.raises(RegistrationRejected):
        push_to_url("   ")


def test_push_to_url_maps_server_rejections():
    with pytest.raises(RegistrationRejected) as excinfo:
        push_to_url(
            "https://api.day.app/KEY",
            body="hello",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    400, json={"code": 400, "message": "invalid device key"}
                )
            ),
        )
    assert "invalid device key" in str(excinfo.value)

    with pytest.raises(ServerRejected):
        push_to_url(
            "https://api.day.app/KEY",
            transport=httpx.MockTransport(lambda request: httpx.Response(403, json={})),
        )


def test_push_to_url_maps_transport_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(ServerUnreachable) as excinfo:
        push_to_url("https://api.day.app/KEY", transport=httpx.MockTransport(handler))
    assert "api.day.app" in str(excinfo.value)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(ServerTimeout):
        push_to_url("https://api.day.app/KEY", transport=httpx.MockTransport(timeout))
