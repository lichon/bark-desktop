"""Logging setup with aggressive redaction of secrets and message content.

Logs must stay useful for diagnosing reconnects and registration failures without
ever containing tokens, certificates, DPAPI blobs or notification bodies
(FR-026).  The redaction filter is applied to every record regardless of the
handler that ultimately emits it.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

REDACTED = "[redacted]"

# 64+ hex characters: base push tokens, hashes of long material.
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{64,}\b")
# Long base64 blobs (certificates, DPAPI payloads).
_BASE64_BLOB = re.compile(r"(?:[A-Za-z0-9+/]{60,}={0,2})")
# key=value style secrets.
_KV_SECRET = re.compile(
    r"(?i)\b(token|authorization|api[_-]?key|supabase_key|certificate|private[_-]?key|"
    r"cert_pem|key_pem|device_token|base_token_hex)\b\s*[:=]\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s,)]+)"
)
# PEM blocks (may span multiple lines) - handled per line by the filter.
_PEM_BLOCK = re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL)
# JSON-style secret fields.
_JSON_SECRET = re.compile(
    r'(?i)"(token|authorization|supabase_key|cert_pem|key_pem|device_token|'
    r'base_token_hex|body)"'
    r"\s*:\s*(\"[^\"]*\"|[^,}\s]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
# Python bytes reprs (`pypush` logs whole commands: payloads and tokens). Short
# capability blobs stay readable; anything longer is notification material.
_BYTES_REPR = re.compile(r"""b(?:"(?:[^"\\]|\\.){16,}"|'(?:[^'\\]|\\.){16,}')(?=[,)\]\s]|$)""")

SENSITIVE_EXTRA_KEYS = frozenset({"body", "title", "subtitle", "raw_json", "supabase_key"})

# `pypush` 2.0.2 warns about every APNs `SendMessage` field its field table does
# not know and embeds the whole packet in the message, payload bytes included.
# Apple now sends field 24 (the notification UUID) on every push, so this fires
# once per notification; the parser skips unknown fields and delivery is
# unaffected.  The warning is therefore downgraded to a body-free debug line.
_UPSTREAM_FIELD_NOISE = re.compile(r"^Unexpected field with packet ID (\d+) in packet ")


def _upstream_field_noise_id(record: logging.LogRecord) -> str | None:
    """Field ID when *record* is pypush's unknown-`SendMessage`-field warning."""
    if record.name != "root":
        return None
    if not (record.pathname or "").replace("\\", "/").endswith(
        "pypush/apns/_protocol.py"
    ):
        return None
    try:
        match = _UPSTREAM_FIELD_NOISE.match(record.getMessage())
    except Exception:  # pragma: no cover - defensive, ``%`` args are unused there
        return None
    return match.group(1) if match else None


# Third-party loggers bridged into the app log while debug level is active:
# `httpx` reports every request/response, `httpcore` the connection details.
BRIDGED_LOGGERS = ("httpx", "httpcore")
_BRIDGE_MARK = "_bark_library_bridge"
_PREVIOUS_ROOT_LEVEL = "_bark_previous_root_level"


def redact_text(text: str) -> str:
    """Return *text* with credentials and raw blobs masked."""
    if not text:
        return text
    mask = lambda m: f"{m.group(1)}={REDACTED}"  # noqa: E731
    text = _PEM_BLOCK.sub(REDACTED, text)
    text = _BYTES_REPR.sub("b'" + REDACTED + "'", text)
    text = _JSON_SECRET.sub(mask, text)
    text = _KV_SECRET.sub(mask, text)
    text = _BEARER.sub(f"Bearer {REDACTED}", text)
    text = _HEX_RUN.sub(REDACTED, text)
    text = _BASE64_BLOB.sub(REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Mask secrets and message bodies on every log record."""

    def __init__(self, redact_bodies: bool = True):
        super().__init__()
        self.redact_bodies = redact_bodies

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - stdlib API
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            message = str(record.msg)
        message = redact_text(message)
        record.msg = message
        record.args = ()
        if self.redact_bodies and record.__dict__.get("notification_body"):
            record.notification_body = REDACTED
        return True


def build_logger(
    log_file: Path | None,
    *,
    level: str = "info",
    console: bool = False,
    max_bytes: int = 2 * 1024 * 1024,
    backups: int = 3,
) -> logging.Logger:
    """Create (or replace) the application logger."""
    logger = logging.getLogger("bark_pc")
    logger.setLevel(_level_value(level))
    logger.propagate = False
    _unbridge_loggers()
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - defensive
            pass

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    redaction = RedactionFilter()

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction)
        logger.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redaction)
        logger.addHandler(stream)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    elif _level_value(level) <= logging.DEBUG:
        # Debug runs also record every `httpx` request/response and `httpcore`
        # connection detail from libraries we do not build clients for.
        for handler in list(logger.handlers):
            _bridge_loggers(handler)
    _bridge_root_logger(logger)
    return logger


class _RootForwarder(logging.Handler):
    """Route module-level ``logging.<level>()`` calls into the app log.

    `pypush` reports courier events (keepalives, received commands, token
    changes) through the root logger because its modules use the module-level
    logging functions.  Those records arrive with the name ``root``; the
    forwarder renames the pypush ones and hands them to the app logger so the
    app's handlers and their redaction filter stay in charge.

    Installed at every level, not only for debug runs: ``logging.warning()``
    calls ``basicConfig()`` when the root logger has no handlers, which would
    print an unformatted, unredacted copy of the record to stderr (whole APNs
    packets and notification bodies included) and leave that handler behind.
    """

    def __init__(self, target: logging.Logger):
        super().__init__(target.level)
        self.target = target

    def emit(self, record: logging.LogRecord) -> None:
        if record.name != "root":
            return
        field_id = _upstream_field_noise_id(record)
        if field_id is not None:
            # Drop it at info level; keep one short, body-free line in debug
            # logs so a genuinely new protocol field is still noticeable.
            if not self.target.isEnabledFor(logging.DEBUG):
                return
            record.msg = (
                f"courier: SendMessage field {field_id} is not mapped by pypush (ignored)"
            )
            record.args = ()
            record.levelno = logging.DEBUG
            record.levelname = "DEBUG"
        pathname = (record.pathname or "").replace("\\", "/")
        if "pypush" in pathname:
            record.name = f"pypush.{record.module}"
        self.target.handle(record)


def _unbridge_loggers() -> None:
    """Detach bridged handlers so a rebuilt logger never writes twice."""
    for name in BRIDGED_LOGGERS:
        library = logging.getLogger(name)
        for handler in list(library.handlers):
            if getattr(handler, _BRIDGE_MARK, False):
                library.removeHandler(handler)
        library.setLevel(logging.NOTSET)
        library.propagate = True
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _BRIDGE_MARK, False):
            root.removeHandler(handler)
            previous = getattr(handler, _PREVIOUS_ROOT_LEVEL, logging.WARNING)
            root.setLevel(previous)


def _bridge_loggers(handler: logging.Handler) -> None:
    """Send `httpx`/`httpcore` records through the app log at debug level."""
    handler_marked = getattr(handler, _BRIDGE_MARK, False)
    if not handler_marked:
        setattr(handler, _BRIDGE_MARK, True)
    for name in BRIDGED_LOGGERS:
        library = logging.getLogger(name)
        library.setLevel(logging.DEBUG)
        library.propagate = False
        library.addHandler(handler)


def _bridge_root_logger(target: logging.Logger) -> None:
    """Capture module-level logging calls (pypush's courier output)."""
    root = logging.getLogger()
    forwarder = _RootForwarder(target)
    setattr(forwarder, _BRIDGE_MARK, True)
    setattr(forwarder, _PREVIOUS_ROOT_LEVEL, root.level)
    root.addHandler(forwarder)
    # Keep the root logger at the app level: debug records from libraries we do
    # not bridge are not interesting, and ``logging.debug()`` must stay cheap.
    root.setLevel(target.level)


def _level_value(level: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(str(level).lower(), logging.INFO)


def log_files(logs_dir: Path) -> list[Path]:
    """Return the three rotated log files that exist, newest first."""
    if not logs_dir.exists():
        return []
    candidates = [logs_dir / "app.log"] + [logs_dir / f"app.log.{i}" for i in range(1, 4)]
    return [path for path in candidates if path.exists()]


def export_diagnostics(logs_dir: Path, settings_snapshot: dict, target_dir: Path) -> Path:
    """Zip redacted logs plus a redacted settings snapshot for support."""
    import json
    import zipfile

    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / "bark-pc-diagnostics.zip"
    safe_settings = {
        key: (REDACTED if key in SENSITIVE_EXTRA_KEYS else value)
        for key, value in settings_snapshot.items()
    }
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in log_files(logs_dir):
            archive.write(path, f"logs/{path.name}")
        archive.writestr(
            "settings.json",
            redact_text(json.dumps(safe_settings, indent=2, sort_keys=True)),
        )
    return destination
