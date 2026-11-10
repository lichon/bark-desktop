"""Native Windows toast presentation.

`InteractableWindowsToaster` is required (not `WindowsToaster`) because message
images are shown with `ToastImagePosition.Hero`.  Every toast belongs to a group
so a `delete` push can dismiss it, and clicks either open the message URL or the
`barkpc:` URI that restores the app.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from ...core.models import Notification, truncate_lines

logger = logging.getLogger("bark_pc.toast")

APP_NAME = "Bark Desktop Client"
HIDDEN_PREVIEW_TEXT = ("Bark Desktop Client", "New notification")


def _load_toasts():
    """Import WinRT toast modules lazily so non-Windows imports never fail."""
    from windows_toasts import (  # noqa: PLC0415 - intentional lazy import
        InteractableWindowsToaster,
        Toast,
        ToastDisplayImage,
        ToastImagePosition,
        UnsupportedOSVersionException,
    )

    return (
        InteractableWindowsToaster,
        Toast,
        ToastDisplayImage,
        ToastImagePosition,
        UnsupportedOSVersionException,
    )


def is_openable_url(url: str | None) -> bool:
    if not url:
        return False
    lowered = url.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


# Windows reports `ToastNotification.add_failed` with an HRESULT and no message,
# so `windows_toasts` hands the raw WinRT event over.  These are the codes that
# actually turn up for an unpackaged app, with what the user can do about them.
TOAST_FAILURE_HINTS = {
    0x803E0114: (
        "notifications are turned off for this Windows user "
        "(Settings > System > Notifications, or ToastEnabled=0 under "
        "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\PushNotifications)"
    ),
    0x803E0105: "the Windows notification platform is unavailable",
    0x803E0207: (
        "Windows dropped the toast without showing it "
        "(Do Not Disturb / Focus Assist, or the notification queue is full)"
    ),
    0x80070490: (
        "Windows has no registration (AUMID) for the toast source; "
        "the toaster falls back to the Command Prompt identifier"
    ),
    0x80070005: "access denied while posting the toast (notifications disabled by policy)",
    0x8007000E: "out of memory while posting the toast",
}


def failure_code(event) -> int | None:
    """Normalise WinRT's ``HResult`` (a signed object) to an unsigned code."""
    raw = getattr(event, "error_code", None)
    if raw is None:  # `windows_toasts` < 1.3 handed over a plain object
        raw = getattr(event, "error_message", None)
    for candidate in (raw, getattr(raw, "value", None)):
        if isinstance(candidate, int):
            return candidate & 0xFFFFFFFF
    return None


def describe_toast_failure(code: int | None) -> str:
    """One log-safe line: the HRESULT plus the likely cause and the fix."""
    if code is None:
        return "Windows reported a toast failure without an error code"
    hint = TOAST_FAILURE_HINTS.get(code)
    if hint is None:
        return f"0x{code:08X} (unrecognised Windows notification error)"
    return f"0x{code:08X}: {hint}"


class WindowsToastNotifier:
    """Implements the `Notifier` protocol using native Windows toasts."""

    def __init__(
        self,
        *,
        app_name: str = APP_NAME,
        toasts_enabled: bool = True,
        hide_preview: bool = False,
        image_resolver: Callable[[Notification], Path | None] | None = None,
        on_activated: Callable[[dict], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
    ):
        self.app_name = app_name
        self.toasts_enabled = toasts_enabled
        self.hide_preview = hide_preview
        self.image_resolver = image_resolver
        self.on_activated = on_activated
        self.on_failed = on_failed
        self._toaster = None
        self._live: dict[str, object] = {}
        self._unsupported_logged = False
        self._classes = None
        # Last failure Windows reported asynchronously, as a readable line.
        self.last_failure: str | None = None

    # -- setup ------------------------------------------------------------- #
    def _ensure_toaster(self):
        if self._toaster is not None:
            return self._toaster
        classes = _load_toasts()
        self._classes = classes
        toaster_class = classes[0]
        self._toaster = toaster_class(self.app_name)
        return self._toaster

    def available(self) -> bool:
        try:
            self._ensure_toaster()
            return True
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("toasts unavailable: %s", exc)
            return False

    # -- presentation ------------------------------------------------------ #
    def _text_fields(self, notification: Notification) -> list[str]:
        if self.hide_preview:
            return [HIDDEN_PREVIEW_TEXT[0], HIDDEN_PREVIEW_TEXT[1]]
        title = notification.toast_title or "Bark"
        body = notification.toast_body
        return [title, body]

    def show(self, notification: Notification) -> bool:
        if not self.toasts_enabled:
            logger.debug("toasts disabled; not showing %s", notification.remote_id)
            return False
        _, Toast, ToastDisplayImage, ToastImagePosition, unsupported = _load_toasts()
        try:
            toaster = self._ensure_toaster()
        except Exception as exc:  # pragma: no cover - platform dependent
            logger.warning("cannot create toaster: %s", exc)
            return False

        launch_action = (
            notification.url
            if is_openable_url(notification.url)
            else self._launch_uri(notification)
        )
        toast = Toast(
            text_fields=self._text_fields(notification),
            group=notification.group_name or "bark",
            launch_action=launch_action,
            on_activated=self._handle_activated,
            on_failed=self._handle_failed,
        )
        image_path = self._resolve_image(notification)
        if image_path is not None:
            try:
                toast.AddImage(
                    ToastDisplayImage.fromPath(image_path, position=ToastImagePosition.Hero)
                )
            except Exception as exc:  # pragma: no cover - image/format specific
                logger.info("hero image could not be attached: %s", exc)
        try:
            toaster.show_toast(toast)
        except unsupported:
            if not self._unsupported_logged:
                logger.warning(
                    "this Windows version does not support native toasts; "
                    "notifications are still recorded in history"
                )
                self._unsupported_logged = True
            return False
        except Exception as exc:  # pragma: no cover - WinRT failure
            logger.warning("toast failed: %s", exc)
            return False
        key = self._key(notification)
        self._live[key] = toast
        return True

    def remove(self, notification: Notification) -> None:
        if not self._live:
            return
        key = self._key(notification)
        toast = self._live.pop(key, None)
        if toast is None and notification.remote_id:
            toast = self._live.pop(f"remote:{notification.remote_id}", None)
        if toast is None:
            return
        try:
            toaster = self._ensure_toaster()
            toaster.remove_toast(toast)
        except Exception as exc:  # pragma: no cover - toast already gone
            logger.debug("could not dismiss toast: %s", exc)

    def remove_all(self) -> None:
        try:
            toaster = self._ensure_toaster()
            toaster.clear_toasts()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("could not clear toasts: %s", exc)
        self._live.clear()

    # -- helpers ----------------------------------------------------------- #
    @staticmethod
    def _key(notification: Notification) -> str:
        if notification.remote_id:
            return f"remote:{notification.remote_id}"
        if notification.id is not None:
            return f"id:{notification.id}"
        return f"at:{notification.received_at}"

    def _launch_uri(self, notification: Notification) -> str:
        if notification.id is not None:
            return f"barkpc:open?id={notification.id}"
        return "barkpc:open"

    def _resolve_image(self, notification: Notification) -> Path | None:
        if self.image_resolver is None:
            return None
        try:
            path = self.image_resolver(notification)
        except Exception:  # pragma: no cover - resolver isolation
            logger.exception("image resolver failed")
            return None
        if path is None:
            return None
        path = Path(path)
        return path if path.exists() else None

    def _handle_activated(self, event) -> None:
        payload = {
            "arguments": getattr(event, "arguments", "") or "",
            "input": getattr(event, "input", "") or "",
        }
        logger.info("toast activated")
        if self.on_activated is not None:
            try:
                self.on_activated(payload)
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("toast activation handler failed")

    def _handle_failed(self, event) -> None:
        message = describe_toast_failure(failure_code(event))
        self.last_failure = message
        logger.warning("toast failed: %s", message)
        if self.on_failed is not None:
            try:
                self.on_failed(message)
            except Exception:  # pragma: no cover - callback isolation
                logger.exception("toast failure handler failed")


def truncate_for_display(text: str) -> str:
    return truncate_lines(text)
