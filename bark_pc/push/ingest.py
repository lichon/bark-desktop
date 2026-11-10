"""Payload → storage → notification presentation, isolated from the UI.

Keeping this separate from `app.py` lets the unit tests exercise the complete
ingest path (parse → de-duplicate → store → delete/present) with a fake notifier
and a temp database, and keeps the receiver thread free of UI work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..core.db import Database, RecentPayloadFilter
from ..core.models import Notification, now_ms
from .payload import ParseOutcome, parse_payload

logger = logging.getLogger("bark_pc.ingest")


@dataclass
class IngestResult:
    outcome: ParseOutcome
    notification: Notification | None = None
    notification_id: int | None = None
    deleted_remote_id: str | None = None
    duplicate: bool = False
    reason: str | None = None


class Ingestor:
    """Applies one raw push payload to storage and notifies the caller."""

    def __init__(
        self,
        db: Database,
        *,
        notifier=None,
        recent_filter: RecentPayloadFilter | None = None,
        on_image_url: Callable[[Notification], None] | None = None,
        on_stored: Callable[[Notification], None] | None = None,
        on_deleted: Callable[[str], None] | None = None,
    ):
        self.db = db
        self.notifier = notifier
        self.recent_filter = recent_filter or RecentPayloadFilter()
        self.on_image_url = on_image_url
        self.on_stored = on_stored
        self.on_deleted = on_deleted

    def handle_payload(
        self, raw: bytes, *, source_server: str = "", from_self: bool = False
    ) -> IngestResult:
        parsed = parse_payload(raw, source_server=source_server)

        if parsed.outcome == ParseOutcome.EMPTY:
            logger.debug("ignoring push: %s", parsed.reason)
            return IngestResult(outcome=parsed.outcome, reason=parsed.reason)
        if parsed.outcome == ParseOutcome.MALFORMED:
            logger.warning("ignoring malformed push: %s", parsed.reason)
            return IngestResult(outcome=parsed.outcome, reason=parsed.reason)

        if parsed.outcome == ParseOutcome.DELETE:
            remote_id = parsed.remote_id or ""
            if not remote_id:
                return IngestResult(outcome=ParseOutcome.EMPTY, reason="delete without id")
            self.db.delete_by_remote_id(source_server, remote_id)
            result = IngestResult(outcome=parsed.outcome, deleted_remote_id=remote_id)
            self._safe(self.on_deleted, remote_id)
            return result

        notification = parsed.notification
        if notification is None:  # pragma: no cover - defensive
            return IngestResult(outcome=ParseOutcome.MALFORMED, reason="no notification")

        if from_self:
            notification.from_self = True
        if notification.from_self and notification.read_at is None:
            # The user already saw this message when they sent it: no badge, no
            # toast, no speech.  It still lands in the history, marked read.
            notification.read_at = now_ms()

        if notification.remote_id is None and self.recent_filter.is_duplicate(
            source_server, parsed.raw
        ):
            logger.debug("ignoring duplicate payload without an id")
            return IngestResult(outcome=parsed.outcome, notification=notification, duplicate=True)

        notification_id = self.db.insert_notification(notification)
        notification.id = notification_id
        logger.debug(
            "stored notification %s (remote id %s, outcome %s)",
            notification_id,
            notification.remote_id,
            parsed.outcome.value,
        )
        result = IngestResult(
            outcome=parsed.outcome,
            notification=notification,
            notification_id=notification_id,
            duplicate=notification.id != notification_id,
        )

        if notification.image_url and self.on_image_url is not None:
            self._safe(self.on_image_url, notification)
        if self.notifier is not None and not notification.from_self:
            self._safe(self.notifier.show, notification)
        if self.on_stored is not None:
            self._safe(self.on_stored, notification)
        return result

    @staticmethod
    def _safe(callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # pragma: no cover - callbacks must never break ingest
            logger.exception("ingest callback failed")
