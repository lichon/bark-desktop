"""A compose-bar message must always be able to reach this device.

When *Settings → Message send* has no usable route (the default is the
Realtime room, which may be off and whose Bark URL field may be empty), the
message falls back to a plain Bark push to the device key registered during
setup.  Only a device with neither a route nor a device key cannot send.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from bark_pc import app as app_module
from bark_pc.app import BarkApp
from bark_pc.core.config import (
    MESSAGE_ROUTE_BARK,
    MESSAGE_ROUTE_BOTH,
    MESSAGE_ROUTE_REALTIME,
    Settings,
)
from bark_pc.core.models import BarkLink, LinkState

LOG = logging.getLogger("bark_pc.tests.self_message")

REALTIME_CONFIG = ("sb://project.supabase.co", "anon-key", "bark")


class FakeDatabase:
    def __init__(self, link: BarkLink | None):
        self.link = link

    def get_link(self) -> BarkLink | None:
        return self.link


class StubApp:
    """Just the self-message methods, without building the whole app."""

    _self_push_url = BarkApp._self_push_url
    _message_targets = BarkApp._message_targets
    can_send_message = BarkApp.can_send_message
    send_message = BarkApp.send_message

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        link: BarkLink | None = None,
        supabase_connected: bool = False,
        supabase_config: tuple[str, str, str] | None = REALTIME_CONFIG,
    ):
        self.settings = settings or Settings()
        self.db = FakeDatabase(link)
        self.log = LOG
        self._supabase_connected = supabase_connected
        self._supabase_config_value = supabase_config
        self.published: list[tuple] = []

    def is_supabase_connected(self) -> bool:
        return self._supabase_connected

    def _supabase_config(self):
        return self._supabase_config_value

    def _publish_self_message(self, *args) -> None:  # pragma: no cover - faked
        self.published.append(args)


class FakeThread:
    """Runs nothing; records what the worker would have done."""

    def __init__(self, *, target, args=(), **_kwargs):
        self.target = target
        self.args = args

    def start(self) -> None:
        self.target(*self.args)


@pytest.fixture
def linked_device() -> BarkLink:
    return BarkLink(
        server_url="https://api.day.app",
        device_key="deviceKEY",
        state=LinkState.LINKED,
    )


def _settings(**overrides) -> Settings:
    settings = Settings()
    for name, value in overrides.items():
        setattr(settings, name, value)
    return settings


def test_self_push_url_uses_the_linked_device(linked_device):
    app = StubApp(link=linked_device)

    assert app._self_push_url() == "https://api.day.app/deviceKEY"


@pytest.mark.parametrize(
    "link",
    [
        None,
        BarkLink(server_url="https://api.day.app", device_key=""),
        BarkLink(server_url="sb://project.supabase.co", device_key="deviceKEY"),
        BarkLink(
            server_url="https://api.day.app",
            device_key="deviceKEY",
            state=LinkState.ABANDONED,
        ),
    ],
    ids=["no-link", "no-device-key", "supabase-link", "abandoned-link"],
)
def test_self_push_url_is_empty_without_a_bark_device_key(link):
    app = StubApp(link=link)

    assert app._self_push_url() == ""


def test_default_route_falls_back_to_this_device(linked_device):
    app = StubApp(settings=_settings(message_route=MESSAGE_ROUTE_REALTIME), link=linked_device)

    realtime_config, bark_url, fallback = app._message_targets()

    assert realtime_config is None
    assert bark_url == "https://api.day.app/deviceKEY"
    assert fallback is True
    assert app.can_send_message() is True


def test_configured_bark_url_wins_over_the_fallback(linked_device):
    app = StubApp(
        settings=_settings(
            message_route=MESSAGE_ROUTE_BARK,
            message_route_url="https://push.example.com/otherKEY",
        ),
        link=linked_device,
    )

    realtime_config, bark_url, fallback = app._message_targets()

    assert realtime_config is None
    assert bark_url == "https://push.example.com/otherKEY"
    assert fallback is False


def test_empty_bark_url_field_falls_back_to_this_device(linked_device):
    app = StubApp(settings=_settings(message_route=MESSAGE_ROUTE_BARK), link=linked_device)

    _realtime_config, bark_url, fallback = app._message_targets()

    assert bark_url == "https://api.day.app/deviceKEY"
    assert fallback is True


def test_realtime_room_is_preferred_while_it_is_connected(linked_device):
    app = StubApp(
        settings=_settings(message_route=MESSAGE_ROUTE_BOTH),
        link=linked_device,
        supabase_connected=True,
    )

    realtime_config, bark_url, fallback = app._message_targets()

    assert realtime_config == REALTIME_CONFIG
    assert bark_url == ""
    assert fallback is False


def test_stale_link_still_falls_back_to_this_device():
    app = StubApp(
        link=BarkLink(
            server_url="https://api.day.app",
            device_key="deviceKEY",
            state=LinkState.STALE,
        )
    )

    assert app.can_send_message() is True
    assert app._message_targets()[1] == "https://api.day.app/deviceKEY"


def test_nothing_can_send_without_a_route_or_a_device_key():
    app = StubApp(settings=_settings(message_route=MESSAGE_ROUTE_REALTIME))

    assert app.can_send_message() is False
    assert app.send_message("hello") is False


def test_send_message_pushes_to_the_device_key_when_unconfigured(
    monkeypatch, linked_device
):
    app = StubApp(settings=_settings(message_route=MESSAGE_ROUTE_REALTIME), link=linked_device)
    monkeypatch.setattr(app_module, "threading", SimpleNamespace(Thread=FakeThread))

    assert app.send_message("  hello there  ") is True

    assert app.published == [("hello there", None, "https://api.day.app/deviceKEY")]


def test_send_message_uses_the_configured_bark_url(monkeypatch, linked_device):
    app = StubApp(
        settings=_settings(
            message_route=MESSAGE_ROUTE_BARK,
            message_route_url="https://push.example.com/otherKEY",
        ),
        link=linked_device,
    )
    monkeypatch.setattr(app_module, "threading", SimpleNamespace(Thread=FakeThread))

    assert app.send_message("hi") is True

    assert app.published == [("hi", None, "https://push.example.com/otherKEY")]


def test_send_message_ignores_blank_text(monkeypatch, linked_device):
    app = StubApp(link=linked_device)
    monkeypatch.setattr(app_module, "threading", SimpleNamespace(Thread=FakeThread))

    assert app.send_message("   ") is False
    assert app.published == []
