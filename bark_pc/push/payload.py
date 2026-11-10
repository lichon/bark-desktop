"""APNs/Bark payload → `Notification` mapping.

Every rule here comes from `contracts/push-payload.md`, which is derived from
bark-server's `apns/apns.go`.  Parsing must never raise: unknown fields, missing
`aps`, bare-string alerts and invalid UTF-8 all degrade to a defined outcome
(FR-013).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from ..core import models
from ..core.models import Notification

logger = logging.getLogger("bark_pc.payload")

ENCRYPTED_PLACEHOLDER = "Encrypted message — not supported in this version"
# Internal custom field the Supabase transport adds to broadcasts this desktop
# sent itself.  Lower-case, like every custom field the server stringifies.
SELF_FIELD = "bark_desktop_self"
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*(https?://[^)\s]+)\s*\)")


class ParseOutcome(str, Enum):
    MESSAGE = "message"
    DELETE = "delete"
    EMPTY = "empty"
    ENCRYPTED = "encrypted"
    MALFORMED = "malformed"


@dataclass
class ParsedPush:
    outcome: ParseOutcome
    notification: Notification | None = None
    remote_id: str | None = None
    reason: str | None = None
    raw: bytes = b""
    payload: dict = field(default_factory=dict)

    @property
    def is_message(self) -> bool:
        return self.outcome == ParseOutcome.MESSAGE

    @property
    def is_delete(self) -> bool:
        return self.outcome == ParseOutcome.DELETE


def _stringify(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


def _as_text(value) -> str:
    return _stringify(value).strip()


def _is_flag(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    return str(value).strip().lower() in ("1", "true")


def _is_http_url(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.strip().lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def extract_markdown_image(body: str) -> str | None:
    match = _MARKDOWN_IMAGE.search(body or "")
    if not match:
        return None
    url = match.group(1)
    return url if _is_http_url(url) else None


def normalize_custom_fields(payload: dict) -> dict[str, str]:
    """Lower-case and stringify every custom field (bark-server behaviour)."""
    custom: dict[str, str] = {}
    for key, value in payload.items():
        text_key = _stringify(key).lower()
        if text_key == "aps":
            continue
        custom[text_key] = _stringify(value)
    return custom


def parse_payload(
    raw: bytes | str,
    *,
    source_server: str = "",
    received_at: int | None = None,
) -> ParsedPush:
    """Parse one courier payload into a `ParsedPush`; never raises."""
    raw_bytes = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else raw or b""
    text = raw_bytes.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except (ValueError, TypeError) as exc:
        return ParsedPush(
            outcome=ParseOutcome.MALFORMED,
            reason=f"invalid JSON: {exc}",
            raw=raw_bytes,
        )
    if not isinstance(payload, dict):
        return ParsedPush(
            outcome=ParseOutcome.MALFORMED,
            reason=f"payload is {type(payload).__name__}, expected an object",
            raw=raw_bytes,
        )

    aps_raw = payload.get("aps")
    aps = aps_raw if isinstance(aps_raw, dict) else {}
    alert = aps.get("alert")

    title = subtitle = body = ""
    if isinstance(alert, dict):
        title = _as_text(alert.get("title"))
        subtitle = _as_text(alert.get("subtitle"))
        body = _as_text(alert.get("body"))
    elif alert is not None and not isinstance(alert, (list, dict)):
        body = _as_text(alert)

    custom = normalize_custom_fields(payload)
    remote_id = custom.get("id") or None
    if remote_id is not None and not remote_id.strip():
        remote_id = None

    # 1. Delete/recall push.
    if _is_flag(custom.get("delete")):
        if not remote_id:
            return ParsedPush(
                outcome=ParseOutcome.EMPTY,
                reason="delete push without id",
                raw=raw_bytes,
                payload=payload,
            )
        return ParsedPush(
            outcome=ParseOutcome.DELETE,
            remote_id=remote_id,
            raw=raw_bytes,
            payload=payload,
        )

    group = custom.get("group") or _as_text(aps.get("thread-id")) or None
    url = custom.get("url") or None
    level = custom.get("level") or None
    from_self = _is_flag(custom.get(SELF_FIELD))
    raw_json = json.dumps(payload, ensure_ascii=False)

    # 2. Encrypted push: never decrypted, still counted as delivered.
    if custom.get("ciphertext"):
        notification = Notification(
            remote_id=remote_id,
            title=title,
            subtitle=subtitle or None,
            body=ENCRYPTED_PLACEHOLDER,
            group_name=group,
            url=url,
            level=level,
            image_url=None,
            raw_json=raw_json,
            received_at=received_at or models.now_ms(),
            source_server=source_server,
            from_self=from_self,
        )
        return ParsedPush(
            outcome=ParseOutcome.ENCRYPTED,
            notification=notification,
            remote_id=remote_id,
            raw=raw_bytes,
            payload=payload,
        )

    # 3. Empty alert: nothing to show, not worth a history row.
    if not title and not subtitle and not body:
        return ParsedPush(
            outcome=ParseOutcome.EMPTY,
            reason="empty alert",
            raw=raw_bytes,
            payload=payload,
        )

    # 4. Image: custom `icon` wins; otherwise the first markdown image.
    image_url = custom.get("icon") or ""
    if not _is_http_url(image_url):
        image_url = extract_markdown_image(body) or ""

    notification = Notification(
        remote_id=remote_id,
        title=title,
        subtitle=subtitle or None,
        body=body,
        group_name=group,
        url=url,
        level=level,
        image_url=image_url or None,
        raw_json=raw_json,
        received_at=received_at or models.now_ms(),
        source_server=source_server,
        from_self=from_self,
    )
    return ParsedPush(
        outcome=ParseOutcome.MESSAGE,
        notification=notification,
        remote_id=remote_id,
        raw=raw_bytes,
        payload=payload,
    )
