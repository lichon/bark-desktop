"""Webhook routing rules: validation, matching, storage and delivery."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from bark_pc.core.config import Paths
from bark_pc.core.models import Notification
from bark_pc.push.routing import (
    RouteStore,
    WebhookDispatcher,
    WebhookRoute,
    format_webhook_url,
    parse_header_lines,
    route_matches,
    validate_headers,
    validate_webhook_url,
    webhook_payload,
)


def _notification(**overrides) -> Notification:
    values = {
        "id": 7,
        "remote_id": "remote-7",
        "title": "Deploy finished",
        "subtitle": "prod",
        "body": "build 412 is live",
        "group_name": "ops",
        "level": "timeSensitive",
        "url": "https://ci.example.com/build/412",
        "image_url": "https://example.com/chart.png",
        "raw_json": json.dumps(
            {"type": "deploy", "title": "Deploy finished", "group": "ops"}
        ),
        "received_at": 1_700_000_000_000,
        "source_server": "api.day.app",
    }
    values.update(overrides)
    return Notification(**values)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_webhook_url_is_canonicalized():
    assert validate_webhook_url("example.com/hooks/ci?token=1") == (
        "https://example.com/hooks/ci?token=1"
    )
    assert validate_webhook_url("http://127.0.0.1:8080/hook/") == (
        "http://127.0.0.1:8080/hook/"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com/hook",
        "https://user:pass@example.com/hook",
        "https://example.com/hook#fragment",
        "https://",
    ],
)
def test_invalid_webhook_urls_are_rejected(url):
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_headers_are_validated_and_parsed():
    assert parse_header_lines("Authorization: Bearer abc\n\nX-Env: prod") == {
        "Authorization": "Bearer abc",
        "X-Env": "prod",
    }
    with pytest.raises(ValueError):
        parse_header_lines("not a header")
    with pytest.raises(ValueError):
        validate_headers({"Bad Header": "x"})
    with pytest.raises(ValueError):
        validate_headers({"X-Test": "line\nbreak"})


def test_route_from_dict_applies_defaults_and_rejects_bad_method():
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "message_group": "ops", "enabled": False}
    )
    assert route.method == "GET"
    assert route.content == "none"
    assert route.message_group == "ops"
    assert route.enabled is False
    with pytest.raises(ValueError):
        WebhookRoute.from_dict({"url": "https://example.com/hook", "method": "DELETE"})


def test_legacy_message_type_is_read_as_the_message_group():
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "message_type": "ops"}
    )
    assert route.message_group == "ops"


def test_route_store_round_trips_and_skips_invalid_entries(portable_paths: Paths):
    store = RouteStore(portable_paths)
    route = WebhookRoute.from_dict(
        {
            "name": "CI",
            "url": "https://example.com/hook",
            "message_group": "ops",
            "title": "prod",
        }
    )
    store.save([route])
    reloaded = RouteStore(portable_paths).load()
    assert len(reloaded) == 1
    assert reloaded[0].name == "CI"
    assert reloaded[0].message_group == "ops"

    portable_paths.routes_path.write_text(
        json.dumps({"version": 1, "routes": [{"url": "ftp://bad"}, route.to_dict()]}),
        encoding="utf-8",
    )
    assert [item.name for item in RouteStore(portable_paths).load()] == ["CI"]


def test_missing_or_corrupt_route_file_means_no_rules(portable_paths: Paths):
    assert RouteStore(portable_paths).load() == []
    portable_paths.routes_path.write_text("{not json", encoding="utf-8")
    assert RouteStore(portable_paths).load() == []


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
def test_message_group_matches_only_the_push_group():
    notification = _notification()
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "message_group": "OPS"}
    )
    assert route_matches(route, notification)

    # The custom type ("deploy") and level ("timeSensitive") are not groups.
    route.message_group = "deploy"
    assert not route_matches(route, notification)
    route.message_group = "timeSensitive"
    assert not route_matches(route, notification)


def test_group_and_title_conditions_are_anded():
    notification = _notification()
    route = WebhookRoute.from_dict(
        {
            "url": "https://example.com/hook",
            "message_group": "ops",
            "title": "PROD",
        }
    )
    assert route_matches(route, notification)
    route.title = "staging"
    assert not route_matches(route, notification)


def test_empty_conditions_match_every_message_and_disabled_rules_do_not():
    notification = _notification()
    route = WebhookRoute.from_dict({"url": "https://example.com/hook"})
    assert route_matches(route, notification)
    route.enabled = False
    assert not route_matches(route, notification)


def test_title_matches_the_subtitle_and_is_case_insensitive():
    notification = _notification(title="Release", subtitle="Prod cluster")
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "title": "prod cluster"}
    )
    assert route_matches(route, notification)


def test_message_group_is_empty_without_a_group():
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "message_group": "ops"}
    )
    assert not route_matches(route, _notification(group_name=None))


# --------------------------------------------------------------------------- #
# Payload and delivery
# --------------------------------------------------------------------------- #
def test_webhook_payload_exposes_the_notification_and_raw_fields():
    notification = _notification()
    route = WebhookRoute.from_dict(
        {"name": "CI", "url": "https://example.com/hook", "content": "full"}
    )
    body = webhook_payload(notification, route)
    assert body["event"] == "bark.notification"
    assert body["route"] == {"id": route.id, "name": "CI"}
    assert "type" not in body
    assert body["group"] == "ops"
    assert body["display_title"] == "Deploy finished — prod"
    assert body["payload"]["type"] == "deploy"
    assert body["received_at_iso"].startswith("2023-11-14T22:13:20")


def test_webhook_payload_omits_message_content_by_default():
    notification = _notification()
    route = WebhookRoute.from_dict({"url": "https://example.com/hook"})
    body = webhook_payload(notification, route)
    for key in ("title", "subtitle", "display_title", "body", "payload"):
        assert key not in body
    assert "type" not in body
    assert "level" not in body
    assert body["group"] == "ops"
    assert body["source"] == "api.day.app"


def test_content_mode_selects_which_message_fields_are_sent():
    notification = _notification()
    route = WebhookRoute.from_dict({"url": "https://example.com/hook"})

    route.content = "title"
    body = webhook_payload(notification, route)
    assert body["title"] == "Deploy finished"
    assert "body" not in body
    assert "payload" not in body

    route.content = "body"
    body = webhook_payload(notification, route)
    assert body["title"] == "Deploy finished"
    assert body["body"] == "build 412 is live"
    assert "payload" not in body

    route.content = "full"
    body = webhook_payload(notification, route)
    assert body["body"] == "build 412 is live"
    assert body["payload"]["type"] == "deploy"


def test_format_webhook_url_fills_and_encodes_placeholders():
    notification = _notification()
    route = WebhookRoute.from_dict(
        {
            "url": "https://example.com/hook/{group}?title={title}&body={body}&image={image_url}",
        }
    )
    url = format_webhook_url(notification, route)
    assert url.startswith("https://example.com/hook/ops?title=Deploy%20finished&body=")
    assert "build%20412%20is%20live" in url
    assert "image=https%3A%2F%2Fexample.com%2Fchart.png" in url


def test_unknown_url_placeholders_are_rejected():
    with pytest.raises(ValueError):
        WebhookRoute.from_dict({"url": "https://example.com/hook/{nope}"})


def test_dispatcher_delivers_to_every_matching_rule_with_headers_and_method():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    route = WebhookRoute.from_dict(
        {
            "url": "https://example.com/hook",
            "method": "PUT",
            "headers": {"Authorization": "Bearer secret"},
        }
    )
    dispatcher = WebhookDispatcher(
        [route], transport=httpx.MockTransport(handler)
    )
    try:
        dispatcher.submit(_notification())
        deadline = time.monotonic() + 3
        while not requests and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        dispatcher.close()

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "PUT"
    assert request.headers["Authorization"] == "Bearer secret"
    assert json.loads(request.content)["group"] == "ops"


def test_default_get_delivery_sends_fields_as_query_parameters():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    route = WebhookRoute.from_dict(
        {
            "url": "https://example.com/hook?token=abc&title={title}&body={body}",
            "content": "body",
        }
    )
    dispatcher = WebhookDispatcher(transport=httpx.MockTransport(handler))
    try:
        response = dispatcher.deliver(route, _notification())
    finally:
        dispatcher.close()

    assert response is not None
    request = requests[0]
    assert request.method == "GET"
    params = request.url.params
    assert params["token"] == "abc"
    assert params["title"] == "Deploy finished"
    assert params["body"] == "build 412 is live"
    for key in ("group", "source", "received_at", "payload"):
        assert key not in params
    assert request.content == b""


def test_dispatcher_skips_self_messages_and_non_matching_rules():
    delivered: list[httpx.Request] = []
    route = WebhookRoute.from_dict(
        {"url": "https://example.com/hook", "message_group": "ops"}
    )
    dispatcher = WebhookDispatcher(
        [route], transport=httpx.MockTransport(
            lambda request: delivered.append(request) or httpx.Response(200)
        )
    )
    try:
        assert dispatcher.submit(_notification(from_self=True)) == 0
        assert dispatcher.submit(_notification(title="Other", group_name="builds")) == 0
        time.sleep(0.1)
    finally:
        dispatcher.close()
    assert delivered == []


def test_http_failures_do_not_raise():
    dispatcher = WebhookDispatcher(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500, text="boom"))
    )
    route = WebhookRoute.from_dict({"url": "https://example.com/hook"})
    try:
        response = dispatcher.deliver(route, _notification())
    finally:
        dispatcher.close()
    assert response is not None
    assert response.status_code == 500
