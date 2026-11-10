"""Single-instance enforcement and the loopback command channel.

A named mutex provides exclusivity (released automatically by the OS when the
process dies), and the primary instance listens on `127.0.0.1` with a one-line
JSON protocol so a second launch or a `barkpc:` URI can focus the running app.

The pure helpers (`build_message`, `parse_message`, `is_stale_record`) are
exported for tests; no real mutex or socket is required to exercise the protocol.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from ...core import models
from ...core.config import atomic_write_text

logger = logging.getLogger("bark_pc.single_instance")

MUTEX_PREFIX = "Local\\BarkPcClient"
COMMANDS = frozenset({"focus", "open", "quit", "setup"})
CONNECTION_TIMEOUT = 2.0
REQUEST_TIMEOUT = 2.0


class InvalidToken(PermissionError):
    """Raised when a command carries the wrong instance token."""


@dataclass
class InstanceRecord:
    pid: int
    port: int
    token: str
    started_at: int

    def to_dict(self) -> dict:
        return {
            "pid": self.pid,
            "port": self.port,
            "token": self.token,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InstanceRecord:
        return cls(
            pid=int(data["pid"]),
            port=int(data["port"]),
            token=str(data["token"]),
            started_at=int(data.get("started_at") or 0),
        )


def current_user_sid() -> str:
    """Return the string SID of the current user, or a stable fallback."""
    if sys.platform != "win32":
        return f"nonwin-{os.environ.get('USER', 'user')}"
    try:
        import ctypes
        from ctypes import wintypes

        TOKEN_QUERY = 0x0008
        token = wintypes.HANDLE()
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
        ):
            raise OSError("OpenProcessToken failed")
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise OSError("GetTokenInformation failed")
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            string_sid = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
                raise OSError("ConvertSidToStringSid failed")
            try:
                return string_sid.value or ""
            finally:
                kernel32.LocalFree(string_sid)
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:  # pragma: no cover - platform dependent
        logger.debug("SID lookup failed (%s); using username fallback", exc)
        return f"{os.environ.get('USERNAME', 'user')}@{os.environ.get('COMPUTERNAME', '')}"


def mutex_name(sid: str | None = None) -> str:
    sid = (sid or current_user_sid()).lower()
    digest = hashlib.sha1(sid.encode("utf-8")).hexdigest()[:16]
    return f"{MUTEX_PREFIX}.{digest}"


# --------------------------------------------------------------------------- #
# Wire protocol helpers (pure, testable)
# --------------------------------------------------------------------------- #


def build_message(token: str, command: str, notification_id: int | None = None) -> str:
    if command not in COMMANDS:
        raise ValueError(f"unknown command: {command}")
    payload: dict = {"token": token, "command": command}
    if notification_id is not None:
        payload["notification_id"] = int(notification_id)
    return json.dumps(payload, separators=(",", ":")) + "\n"


def parse_message(line: str | bytes, expected_token: str | None = None) -> dict:
    """Parse and validate one command line.

    Raises `InvalidToken` for a bad token and `ValueError` for malformed input;
    never raises anything else.
    """
    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("message is not valid UTF-8") from exc
    else:
        text = line
    text = (text or "").strip()
    if not text:
        raise ValueError("empty message")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("message must be a JSON object")
    command = payload.get("command")
    if command not in COMMANDS:
        raise ValueError(f"unknown command: {command!r}")
    if expected_token is not None:
        token = payload.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, expected_token):
            raise InvalidToken("invalid instance token")
    if "notification_id" in payload and payload["notification_id"] is not None:
        try:
            payload["notification_id"] = int(payload["notification_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("notification_id must be an integer") from exc
    return payload


def is_port_open(host: str = "127.0.0.1", port: int = 0, timeout: float = 0.25) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def is_stale_record(record: InstanceRecord | None, port_checker=is_port_open) -> bool:
    """A record is stale when no process is listening on its port."""
    if record is None:
        return False
    return not port_checker("127.0.0.1", record.port)


def read_record(path: Path) -> InstanceRecord | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return InstanceRecord.from_dict(data)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logger.warning("ignoring unreadable %s: %s", path.name, exc)
        return None


def write_record(path: Path, record: InstanceRecord) -> None:
    atomic_write_text(Path(path), json.dumps(record.to_dict(), separators=(",", ":")) + "\n")


def clear_record(path: Path) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("could not remove %s: %s", path, exc)


def send_command(
    record: InstanceRecord,
    command: str,
    *,
    notification_id: int | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> dict:
    """Send one command to the primary instance and return its reply."""
    line = build_message(record.token, command, notification_id)
    with socket.create_connection(("127.0.0.1", record.port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(line.encode("utf-8"))
        reply = b""
        while b"\n" not in reply and len(reply) < 4096:
            chunk = connection.recv(1024)
            if not chunk:
                break
            reply += chunk
    if not reply:
        return {"ok": False, "error": "no reply"}
    try:
        return json.loads(reply.decode("utf-8").strip())
    except ValueError:
        return {"ok": False, "error": "malformed reply"}


class InstanceListener:
    """Loopback listener owned by the primary instance."""

    def __init__(
        self,
        token: str,
        handler,
        *,
        host: str = "127.0.0.1",
        timeout: float = REQUEST_TIMEOUT,
        logger_: logging.Logger | None = None,
    ):
        self.token = token
        self.handler = handler
        self.host = host
        self.timeout = timeout
        self.logger = logger_ or logger
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.port = 0

    def start(self) -> int:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, 0))
        self._socket.listen(4)
        self._socket.settimeout(0.5)
        self.port = int(self._socket.getsockname()[1])
        self._thread = threading.Thread(
            target=self._serve, name="bark-instance-listener", daemon=True
        )
        self._thread.start()
        self.logger.debug("instance listener on %s:%s", self.host, self.port)
        return self.port

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                connection.settimeout(self.timeout)
                line = self._read_line(connection)
                try:
                    payload = parse_message(line, self.token)
                except InvalidToken:
                    self.logger.warning("refused instance command with invalid token")
                    self._reply(connection, {"ok": False, "error": "invalid token"})
                    continue
                except ValueError as exc:
                    self.logger.warning("rejected malformed instance command: %s", exc)
                    self._reply(connection, {"ok": False, "error": "malformed"})
                    continue
                try:
                    result = self.handler(payload) or {"ok": True}
                except Exception:  # pragma: no cover - defensive
                    self.logger.exception("instance command handler failed")
                    result = {"ok": False, "error": "handler failed"}
                self._reply(connection, result)
            finally:
                try:
                    connection.close()
                except OSError:  # pragma: no cover - defensive
                    pass

    @staticmethod
    def _read_line(connection: socket.socket) -> bytes:
        data = b""
        while b"\n" not in data and len(data) < 8192:
            chunk = connection.recv(1024)
            if not chunk:
                break
            data += chunk
        return data

    @staticmethod
    def _reply(connection: socket.socket, payload: dict) -> None:
        try:
            connection.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError:  # pragma: no cover - peer gone
            pass

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:  # pragma: no cover - defensive
                pass
            self._socket = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> InstanceListener:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()


class SingleInstanceManager:
    """Mutual exclusion + record file + listener, with stale-file recovery."""

    def __init__(
        self,
        instance_path: Path,
        *,
        sid: str | None = None,
        token_factory=None,
        port_checker=is_port_open,
        logger_: logging.Logger | None = None,
    ):
        self.instance_path = Path(instance_path)
        self.sid = sid
        self.token_factory = token_factory or (lambda: secrets.token_hex(32))
        self.port_checker = port_checker
        self.logger = logger_ or logger
        self.record: InstanceRecord | None = None
        self.listener: InstanceListener | None = None
        self._mutex_handle = None

    # -- mutex ------------------------------------------------------------- #
    def acquire(self) -> bool:
        """Return True when this process is the primary instance."""
        if sys.platform != "win32":  # pragma: no cover - non-Windows dev runs
            return True
        import ctypes

        name = mutex_name(self.sid)
        ERROR_ALREADY_EXISTS = 183
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
        last_error = ctypes.GetLastError()
        if not handle:
            self.logger.warning("CreateMutexW failed; continuing as primary")
            return True
        if last_error == ERROR_ALREADY_EXISTS:
            ctypes.windll.kernel32.CloseHandle(handle)
            return False
        self._mutex_handle = handle
        return True

    def release(self) -> None:
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        clear_record(self.instance_path)
        self.record = None
        if self._mutex_handle is not None and sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:  # pragma: no cover - defensive
                pass
            self._mutex_handle = None

    # -- record + listener ------------------------------------------------- #
    def existing_record(self) -> InstanceRecord | None:
        record = read_record(self.instance_path)
        if record is not None and is_stale_record(record, self.port_checker):
            self.logger.info("removing stale instance record (pid %s)", record.pid)
            clear_record(self.instance_path)
            return None
        return record

    def start_listener(self, handler) -> int:
        token = self.token_factory()
        listener = InstanceListener(token, handler, logger_=self.logger)
        port = listener.start()
        self.listener = listener
        self.record = InstanceRecord(
            pid=os.getpid(), port=port, token=token, started_at=models.now_ms()
        )
        write_record(self.instance_path, self.record)
        return port

    def focus_existing(self, command: str = "focus", notification_id: int | None = None) -> bool:
        record = read_record(self.instance_path)
        if record is None:
            return False
        try:
            reply = send_command(
                record, command, notification_id=notification_id, timeout=CONNECTION_TIMEOUT
            )
        except OSError as exc:
            self.logger.info("could not reach the running instance: %s", exc)
            return False
        return bool(reply.get("ok"))
