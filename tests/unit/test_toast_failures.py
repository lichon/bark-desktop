"""Toast failure decoding: WinRT reports an HRESULT, not a message.

`windows_toasts` forwards the raw `ToastFailedEventArgs`, so
`getattr(event, "error_message", str(event))` used to log
`<ToastFailedEventArgs object at 0x…>` — no code, no cause, no action.  These
tests pin the replacement (pure functions, no WinRT toast is shown).
"""

from __future__ import annotations

import logging

import pytest

from bark_pc.platform.windows.toast import (
    TOAST_FAILURE_HINTS,
    WindowsToastNotifier,
    describe_toast_failure,
    failure_code,
    is_openable_url,
)

# WPN_E_NOTIFICATION_TYPE_DISABLED — what Windows returns when the user has
# notifications switched off (HKCU ...\PushNotifications\ToastEnabled = 0).
NOTIFICATIONS_OFF = 0x803E0114


class HResult:
    """The shape winrt exposes: a wrapper around a signed int."""

    def __init__(self, value: int):
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - mirrors winrt's repr
        return f"HResult(value={self.value})"


class FailedEvent:
    def __init__(self, code: int | HResult | None):
        if code is not None:
            self.error_code = code


def test_failure_code_reads_winrt_hresult_objects():
    # The value is signed on the wire; the log needs the unsigned HRESULT.
    assert failure_code(FailedEvent(HResult(-2143420140))) == NOTIFICATIONS_OFF
    assert failure_code(FailedEvent(NOTIFICATIONS_OFF)) == NOTIFICATIONS_OFF


def test_failure_code_is_none_without_an_error_code():
    assert failure_code(FailedEvent(None)) is None
    assert failure_code(object()) is None


def test_describe_known_code_names_the_cause_and_the_setting():
    message = describe_toast_failure(NOTIFICATIONS_OFF)

    assert message.startswith("0x803E0114")
    assert "notifications are turned off" in message
    assert "PushNotifications" in message


def test_describe_unknown_code_keeps_the_hex_code():
    message = describe_toast_failure(0x803E0999)

    assert message == "0x803E0999 (unrecognised Windows notification error)"


def test_describe_without_a_code_says_so():
    assert describe_toast_failure(None) == (
        "Windows reported a toast failure without an error code"
    )


@pytest.mark.parametrize("code", sorted(TOAST_FAILURE_HINTS))
def test_every_known_code_renders_as_hex_plus_hint(code):
    message = describe_toast_failure(code)

    assert message.startswith(f"0x{code:08X}: ")
    assert len(message) > len(f"0x{code:08X}: ")


def test_handler_logs_the_code_and_calls_back(caplog):
    reports: list[str] = []
    notifier = WindowsToastNotifier(on_failed=reports.append, toasts_enabled=False)

    with caplog.at_level(logging.WARNING, logger="bark_pc.toast"):
        notifier._handle_failed(FailedEvent(HResult(-2143420140)))

    assert reports and reports[0].startswith("0x803E0114")
    assert notifier.last_failure == reports[0]
    assert "toast failed: 0x803E0114" in caplog.text
    # The old log line carried this unhelpful repr instead of a cause.
    assert "ToastFailedEventArgs object" not in caplog.text


def test_handler_survives_a_failing_callback():
    def explode(_message: str) -> None:
        raise RuntimeError("callback bug")

    notifier = WindowsToastNotifier(on_failed=explode)

    notifier._handle_failed(FailedEvent(NOTIFICATIONS_OFF))

    assert notifier.last_failure.startswith("0x803E0114")


def test_only_http_urls_become_launch_actions():
    assert is_openable_url("https://example.com/x")
    assert is_openable_url("HTTP://example.com")
    assert not is_openable_url("barkpc:open?id=1")
    assert not is_openable_url(None)
