"""pypush debug output: root-logger bridging and redaction of command reprs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pypush.apns.protocol import SendMessageCommand

from bark_pc.core.logging import (
    BRIDGED_LOGGERS,
    RedactionFilter,
    build_logger,
    redact_text,
)

PYPUSH_LIFECYCLE = "C:/venv/Lib/site-packages/pypush/apns/lifecycle.py"
APP_LIFECYCLE = "C:/repos/bark-pc/bark_pc/push/supervisor.py"


@pytest.fixture(autouse=True)
def restore_root_logger():
    root = logging.getLogger()
    level = root.level
    handlers = list(root.handlers)
    yield
    root.handlers = handlers
    root.setLevel(level)
    for name in BRIDGED_LOGGERS:
        library = logging.getLogger(name)
        library.handlers = []
        library.setLevel(logging.NOTSET)
        library.propagate = True


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def module_record(message: str, *, pathname: str, module: str) -> logging.LogRecord:
    """A record as module-level `logging.debug(...)` creates it (name 'root')."""
    return logging.LogRecord(
        name="root",
        level=logging.DEBUG,
        pathname=pathname,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def command_repr() -> str:
    command = SendMessageCommand(
        payload=b'{"aps":{"alert":"secret body"}}',
        id=b"\x01\x02",
        topic="me.fin.bark",
        token=b"\xaa" * 32,
        outgoing=True,
    )
    return repr(command)


# --------------------------------------------------------------------------- #
# Bridging
# --------------------------------------------------------------------------- #


def test_module_level_pypush_logs_reach_the_app_log(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")

    logging.getLogger().handle(
        module_record(
            "Using courier: 1-courier.push.apple.com", pathname=PYPUSH_LIFECYCLE, module="lifecycle"
        )
    )

    content = read(log_path)
    assert "using courier" in content.lower()
    assert "pypush.lifecycle:" in content


def test_module_level_logs_from_other_modules_keep_the_root_name(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")

    logging.getLogger().handle(
        module_record("something else", pathname=APP_LIFECYCLE, module="supervisor")
    )

    assert "root: something else" in read(log_path)


def test_info_level_does_not_capture_module_level_debug_logs(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="info")

    root = logging.getLogger()
    assert not root.isEnabledFor(logging.DEBUG)
    logging.debug("Sending keepalive")

    assert "Sending keepalive" not in read(log_path)


def test_switching_back_to_info_detaches_the_root_forwarder(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")
    build_logger(log_path, level="info")

    root = logging.getLogger()
    assert not any(getattr(h, "_bark_library_bridge", False) for h in root.handlers)
    assert root.level == logging.WARNING


def test_rebuilding_the_logger_keeps_one_root_forwarder(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")
    build_logger(log_path, level="debug")

    root = logging.getLogger()
    forwarders = [h for h in root.handlers if getattr(h, "_bark_library_bridge", False)]
    assert len(forwarders) == 1

    logging.getLogger().handle(
        module_record("Sending keepalive", pathname=PYPUSH_LIFECYCLE, module="lifecycle")
    )
    assert read(log_path).count("Sending keepalive") == 1


# --------------------------------------------------------------------------- #
# Redaction of pypush command reprs
# --------------------------------------------------------------------------- #


def test_command_reprs_are_redacted_in_the_app_log(tmp_path):
    log_path = tmp_path / "logs" / "app.log"
    build_logger(log_path, level="debug")

    logging.getLogger().handle(
        module_record(
            f"Received command: {command_repr()}", pathname=PYPUSH_LIFECYCLE, module="lifecycle"
        )
    )

    content = read(log_path)
    assert "secret body" not in content
    assert "\\xaa\\xaa" not in content
    assert "Received command: SendMessageCommand(payload=b'[redacted]'" in content
    # Short, harmless fields stay readable for debugging.
    assert "topic='me.fin.bark'" in content
    assert "id=b'\\x01\\x02'" in content


def test_hex_tokens_are_redacted():
    assert redact_text("Base token assigned: " + "ab" * 32) == "Base token assigned: [redacted]"


def test_bytes_reprs_shorter_than_sixteen_bytes_are_kept():
    text = "ConnectAck(status=0, capabilities=b'\\x01\\x02')"

    assert redact_text(text) == text


def test_bytes_repr_masking_handles_escaped_quotes():
    text = 'payload=b\'{"aps":{"alert":"a \\"quoted\\" title here"}}\''

    masked = redact_text(text)

    assert "[redacted]" in masked
    assert "quoted" not in masked


def test_bytes_repr_masking_stops_at_the_closing_quote():
    # A greedy match once ate `', capabilities=` between two neighbouring fields.
    text = "unknown5=b'\\x00\\x0b', capabilities=b'\\x00\\x00\\x00\\x03', region='CN'"

    masked = redact_text(text)

    assert "unknown5=b'\\x00\\x0b', capabilities=" in masked
    assert "region='CN'" in masked


def test_redaction_filter_applies_bytes_masking_to_records():
    record = logging.LogRecord(
        name="root",
        level=logging.DEBUG,
        pathname=PYPUSH_LIFECYCLE,
        lineno=1,
        msg="Received command: %s",
        args=(command_repr(),),
        exc_info=None,
    )

    RedactionFilter().filter(record)

    assert "secret body" not in record.getMessage()
    assert "[redacted]" in record.getMessage()
