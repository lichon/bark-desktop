"""Application paths and persisted settings.

User data (database, images, logs, settings, identity) lives in the per-user
application-data folder — `%LOCALAPPDATA%\\BarkDesktopClient` on Windows.
Set `BARK_PC_HOME` to keep everything next to the executable instead (portable
mode), and `BARK_PC_DATA` to point at a custom data folder.  Settings are
validated on load and written atomically so a crash can never leave a truncated
`settings.json`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from ..tts.base import DEFAULT_TTS_PROVIDER, TTS_PROVIDERS
from . import models

APP_DIR_ENV = "BARK_PC_HOME"
DATA_DIR_ENV = "BARK_PC_DATA"
APP_DATA_DIRNAME = "BarkDesktopClient"
DEFAULT_SERVER_URL = "https://api.day.app"
DEFAULT_PUSH_TOPIC = "me.fin.bark"
SETTINGS_FILENAME = "settings.json"
WEBHOOK_ROUTES_FILENAME = "routes.json"
SUPABASE_SCHEME = "sb"
MAX_SUPABASE_KEY_LENGTH = 4096
MAX_MESSAGE_ROUTE_URL_LENGTH = 2048

LOG_LEVELS = ("debug", "info", "warning", "error")

# Where the history window's compose bar sends a message this machine writes.
MESSAGE_ROUTE_REALTIME = "realtime"
MESSAGE_ROUTE_BARK = "bark"
MESSAGE_ROUTE_BOTH = "both"
MESSAGE_ROUTES = (
    MESSAGE_ROUTE_REALTIME,
    MESSAGE_ROUTE_BARK,
    MESSAGE_ROUTE_BOTH,
)
DEFAULT_MESSAGE_ROUTE = MESSAGE_ROUTE_REALTIME
MESSAGE_ROUTE_LABELS = {
    MESSAGE_ROUTE_REALTIME: "Supabase Realtime room",
    MESSAGE_ROUTE_BARK: "Bark URL",
    MESSAGE_ROUTE_BOTH: "Both",
}


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* via a temp file in the same directory + replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - defensive
            pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except Exception:  # pragma: no cover - defensive
            pass
        raise


def resolve_app_dir(executable: str | Path | None = None) -> Path:
    """Installation folder: next to the exe when frozen, repo root otherwise."""
    override = os.environ.get(APP_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    if executable is not None:
        return Path(executable).resolve().parent
    if getattr(sys, "frozen", False):  # pragma: no cover - exercised in packaged runs
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def default_data_dir(app_name: str = APP_DATA_DIRNAME) -> Path:
    """Per-user application-data folder: AppData on Windows, XDG elsewhere."""
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base).expanduser() / app_name
        return Path.home() / "AppData" / "Local" / app_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / app_name
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base).expanduser() / app_name
    return Path.home() / ".local" / "share" / app_name


@dataclass(frozen=True)
class Paths:
    app_dir: Path
    data_root: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.data_root if self.data_root is not None else self.app_dir / "data"

    @property
    def portable_data_dir(self) -> Path:
        """Legacy location used before the move to AppData."""
        return self.app_dir / "data"

    @property
    def images_dir(self) -> Path:
        return self.data_dir / "images"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / SETTINGS_FILENAME

    @property
    def routes_path(self) -> Path:
        return self.data_dir / WEBHOOK_ROUTES_FILENAME

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bark.db"

    @property
    def identity_path(self) -> Path:
        return self.data_dir / "identity.bin"

    @property
    def instance_path(self) -> Path:
        return self.data_dir / "instance.json"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "app.log"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.images_dir, self.logs_dir):
            path.mkdir(parents=True, exist_ok=True)


def resolve_paths(app_dir: str | Path | None = None, *, portable: bool | None = None) -> Paths:
    """Resolve app + data folders.

    Data goes to the per-user AppData folder unless `BARK_PC_HOME`/`portable`
    asks for the old next-to-the-exe layout, or `BARK_PC_DATA` names a folder.
    """
    app = Path(app_dir).expanduser().resolve() if app_dir is not None else resolve_app_dir()
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Paths(app_dir=app, data_root=Path(override).expanduser().resolve())
    if portable is None:
        portable = bool(os.environ.get(APP_DIR_ENV))
    if portable:
        return Paths(app_dir=app, data_root=app / "data")
    return Paths(app_dir=app, data_root=default_data_dir())


def migrate_legacy_data(paths: Paths, logger: logging.Logger | None = None) -> bool:
    """Move a pre-AppData `data/` folder next to the exe into the new location.

    Never overwrites an existing data folder; copies across volumes when a move
    is not possible (e.g. the exe sits on a read-only or removable drive).
    Returns True when data was migrated.
    """
    log = logger or logging.getLogger("bark_pc.config")
    legacy = paths.portable_data_dir
    target = paths.data_dir
    if legacy == target or not legacy.is_dir():
        return False
    try:
        if target.exists() and any(target.iterdir()):
            log.info("legacy data left in %s: %s is already populated", legacy, target)
            return False
    except OSError:  # pragma: no cover - defensive
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.copytree(legacy, target, dirs_exist_ok=True)
            shutil.rmtree(legacy)
        else:
            shutil.move(str(legacy), str(target))
    except OSError as exc:
        log.warning("could not move %s to %s: %s", legacy, target, exc)
        return False
    log.info("moved data from %s to %s", legacy, target)
    return True


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    server_url: str = DEFAULT_SERVER_URL
    push_topic: str = DEFAULT_PUSH_TOPIC
    history_retention_count: int = 500
    history_retention_days: int = 30
    image_cache_mb: int = 200
    toasts_enabled: bool = True
    hide_preview_text: bool = False
    tts_enabled: bool = False
    tts_provider: str = DEFAULT_TTS_PROVIDER
    tts_include_body: bool = True
    tts_only_when_unlocked: bool = True
    tts_rate: int = 0
    tts_volume: int = 100
    tts_max_chars: int = 300
    silence_until: int | None = None
    autostart_enabled: bool = False
    autostart_path: str | None = None
    launch_at_login_configured_at: int | None = None
    last_connected_at: int | None = None
    supabase_enabled: bool = False
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_topic: str = "bark"
    message_route: str = DEFAULT_MESSAGE_ROUTE
    message_route_url: str = ""
    log_level: str = "info"
    window_size: str | None = None
    warnings: list[str] = field(default_factory=list, repr=False, compare=False)

    # -- serialization ----------------------------------------------------- #
    def to_dict(self, *, include_warnings: bool = False) -> dict:
        data = asdict(self)
        if not include_warnings:
            data.pop("warnings", None)
        return data

    @classmethod
    def from_dict(cls, data: dict | None, logger: logging.Logger | None = None) -> Settings:
        settings = cls()
        if not isinstance(data, dict):
            if data is not None:
                settings._warn(logger, f"settings is {type(data).__name__}, using defaults")
            return settings
        for item in fields(cls):
            if item.name in ("warnings",) or item.name not in data:
                continue
            raw = data[item.name]
            value = settings._validate(item.name, raw, logger)
            setattr(settings, item.name, value)
        settings._migrate_legacy_supabase(logger)
        return settings

    def _migrate_legacy_supabase(self, logger: logging.Logger | None) -> None:
        """A pre-dual-transport `sb://` server address becomes the optional room.

        Older builds selected either a Bark server *or* Supabase Realtime with
        `server_url`.  Now the Bark server is always configured and Realtime is
        its own opt-in setting, so move a legacy address across instead of
        leaving the Bark side pointed at a WebSocket room.
        """
        if not is_supabase_url(self.server_url):
            return
        if not self.supabase_url:
            self.supabase_url = self.server_url
        if not self.supabase_topic or self.supabase_topic == "bark":
            self.supabase_topic = self.push_topic or "bark"
        self.supabase_enabled = True
        self.server_url = DEFAULT_SERVER_URL
        self.push_topic = DEFAULT_PUSH_TOPIC
        if logger is not None:
            logger.info(
                "settings: moved the legacy %s link to the optional Supabase room",
                self.supabase_url,
            )

    # -- validation -------------------------------------------------------- #
    def _validate(self, name: str, raw, logger: logging.Logger | None):
        default = getattr(self, name)
        try:
            if name in ("history_retention_count",):
                return _int_in_range(raw, 50, 10_000, default, name, logger, self)
            if name in ("history_retention_days",):
                return _int_in_range(raw, 1, 365, default, name, logger, self)
            if name in ("image_cache_mb",):
                return _int_in_range(raw, 20, 2_000, default, name, logger, self)
            if name == "tts_rate":
                return _int_in_range(raw, -10, 10, default, name, logger, self)
            if name == "tts_volume":
                return _int_in_range(raw, 0, 100, default, name, logger, self)
            if name == "tts_max_chars":
                return _int_in_range(raw, 40, 1_000, default, name, logger, self)
            if name == "tts_provider":
                text = str(raw).strip().lower()
                if text not in TTS_PROVIDERS:
                    raise ValueError("expected " + "|".join(TTS_PROVIDERS))
                return text
            if name in (
                "toasts_enabled",
                "hide_preview_text",
                "autostart_enabled",
                "tts_enabled",
                "tts_include_body",
                "tts_only_when_unlocked",
                "supabase_enabled",
            ):
                if isinstance(raw, bool):
                    return raw
                raise ValueError("expected a boolean")
            if name in ("silence_until", "launch_at_login_configured_at", "last_connected_at"):
                if raw is None:
                    return None
                return int(raw)
            if name == "autostart_path":
                if raw is None or raw == "":
                    return None
                return str(raw)
            if name == "window_size":
                if raw is None or raw == "":
                    return None
                text = str(raw)
                width, _, height = text.partition("x")
                if not (width.isdigit() and height.isdigit()):
                    raise ValueError("expected WxH")
                return text
            if name == "log_level":
                text = str(raw).lower()
                if text not in LOG_LEVELS:
                    raise ValueError("expected debug|info|warning|error")
                return text
            if name == "message_route":
                text = str(raw).strip().lower()
                if text not in MESSAGE_ROUTES:
                    raise ValueError("expected " + "|".join(MESSAGE_ROUTES))
                return text
            if name == "message_route_url":
                return validate_bark_push_url(str(raw or ""))
            if name == "server_url":
                return validate_server_url(str(raw))
            if name == "supabase_url":
                return validate_supabase_url(str(raw or ""))
            if name == "supabase_topic":
                text = str(raw or "").strip()
                if not text:
                    return "bark"
                if len(text) > 155:
                    raise ValueError("expected at most 155 characters")
                return text
            if name == "supabase_key":
                text = str(raw or "").strip()
                if len(text) > MAX_SUPABASE_KEY_LENGTH:
                    raise ValueError(f"expected at most {MAX_SUPABASE_KEY_LENGTH} characters")
                if any(character.isspace() for character in text):
                    raise ValueError("must not contain whitespace")
                return text
            if name == "push_topic":
                text = str(raw).strip()
                if not text or len(text) > 155:
                    raise ValueError("expected 1..155 characters")
                return text
        except (TypeError, ValueError) as exc:
            self._warn(logger, f"invalid value for {name} ({raw!r}): {exc}; using default")
            return default
        return raw

    def _warn(self, logger: logging.Logger | None, message: str) -> None:
        self.warnings.append(message)
        if logger is not None:
            logger.warning("settings: %s", message)


def _int_in_range(raw, low: int, high: int, default: int, name: str, logger, holder) -> int:
    if isinstance(raw, bool):
        raise ValueError("expected an integer")
    value = int(raw)
    if not low <= value <= high:
        raise ValueError(f"expected {low}..{high}")
    return value


def is_supabase_url(raw: str | None) -> bool:
    """True for `sb://…` addresses (a Supabase Realtime project)."""
    text = (raw or "").strip()
    return bool(text) and text.lower().startswith(f"{SUPABASE_SCHEME}://")


def supabase_base_url(raw: str) -> str:
    """`sb://project.supabase.co` → the matching `https://` Supabase project URL.

    Loopback development projects use plain HTTP (`supabase start` serves
    `http://127.0.0.1:54321`), so the scheme follows the host.
    """
    normalized = validate_server_url(raw)
    if not is_supabase_url(normalized):
        raise ValueError("not a sb:// address")
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    scheme = "http" if _is_loopback_host(host) else "https"
    return f"{scheme}://{parts.netloc}{parts.path}"


def validate_supabase_url(raw: str) -> str:
    """Canonicalize an optional Supabase project address (`""` stays empty).

    Accepts `sb://project.supabase.co`, a bare `project.supabase.co`, or the
    project's `https://` URL, which people often paste from the dashboard.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if not text.lower().startswith(f"{SUPABASE_SCHEME}://"):
        if "://" in text:
            parts = urlsplit(text)
            text = f"{SUPABASE_SCHEME}://{parts.netloc}{parts.path}"
        else:
            text = f"{SUPABASE_SCHEME}://{text}"
    return _validate_supabase_url(text)


def validate_bark_push_url(raw: str) -> str:
    """Canonicalize the Bark URL used for self messages (`""` stays empty).

    The value is a Bark push URL that already carries the target device key —
    for example `https://api.day.app/<device_key>` or a self-hosted equivalent.
    A bare host is upgraded to `https`, a trailing slash is dropped, and query
    parameters (Bark's `group=`, `sound=`, …) are preserved.  The message itself
    is POSTed to this URL as JSON, so a path-only push URL works too.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if len(text) > MAX_MESSAGE_ROUTE_URL_LENGTH:
        raise ValueError(f"expected at most {MAX_MESSAGE_ROUTE_URL_LENGTH} characters")
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Bark URL must use http:// or https://")
    if not parts.hostname:
        raise ValueError("Bark URL has no host")
    if parts.username or parts.password:
        raise ValueError("Bark URL must not contain credentials")
    if parts.fragment:
        raise ValueError("Bark URL must not contain a fragment")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"Bark URL has an invalid port: {exc}") from exc
    host = parts.hostname
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port:
        netloc = f"{netloc}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, netloc, path, parts.query, ""))


def _is_loopback_host(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def validate_server_url(raw: str) -> str:
    """Normalize a Bark server URL or raise ValueError explaining the problem.

    `sb://` addresses are still tolerated for reading settings written by older
    builds; `Settings` moves them to the optional Supabase fields on load.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("server address is empty")
    if text.lower().startswith(f"{SUPABASE_SCHEME}://"):
        return _validate_supabase_url(text)
    if "://" not in text:
        text = f"https://{text}"
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise ValueError("server address must use http:// or https://")
    if not parts.hostname:
        raise ValueError("server address has no host")
    if parts.username or parts.password:
        raise ValueError("server address must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("server address must not contain a query or fragment")
    path = parts.path.rstrip("/")
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme}://{netloc}{path}"


def _validate_supabase_url(text: str) -> str:
    """Canonicalize `sb://host[:port][/path]` (also tolerates `sb://https://…`)."""
    remainder = text[len(f"{SUPABASE_SCHEME}://") :].strip()
    if not remainder:
        raise ValueError("Supabase address is empty")
    if remainder.lower().startswith(("http://", "https://")):
        # People paste the project URL after the prefix; accept it as-is.
        inner = urlsplit(remainder)
    else:
        inner = urlsplit(f"//{remainder}")
    if inner.username or inner.password:
        raise ValueError("Supabase address must not contain credentials")
    if inner.query or inner.fragment:
        raise ValueError("Supabase address must not contain a query or fragment")
    host = inner.hostname
    if not host:
        raise ValueError("Supabase address has no host")
    try:
        port = inner.port
    except ValueError as exc:
        raise ValueError(f"Supabase address has an invalid port: {exc}") from exc
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port:
        netloc = f"{netloc}:{port}"
    path = inner.path.rstrip("/")
    return f"{SUPABASE_SCHEME}://{netloc}{path}"


class SettingsStore:
    """Load/save `data/settings.json` with validation and atomic writes."""

    def __init__(self, paths: Paths, logger: logging.Logger | None = None):
        self.paths = paths
        self.logger = logger or logging.getLogger("bark_pc.settings")
        self._settings: Settings | None = None

    @property
    def settings(self) -> Settings:
        if self._settings is None:
            self._settings = self.load()
        return self._settings

    def load(self) -> Settings:
        path = self.paths.settings_path
        data: dict | None = None
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self.logger.warning("settings: cannot read %s: %s; using defaults", path, exc)
                data = None
        settings = Settings.from_dict(data, self.logger)
        self._settings = settings
        return settings

    def save(self, settings: Settings | None = None) -> Settings:
        settings = settings or self.settings
        atomic_write_text(
            self.paths.settings_path,
            json.dumps(settings.to_dict(), indent=2, sort_keys=True) + "\n",
        )
        self._settings = settings
        return settings

    def update(self, **changes) -> Settings:
        settings = self.settings
        known = {item.name for item in fields(Settings)}
        for key, value in changes.items():
            if key != "warnings" and key in known:
                setattr(settings, key, value)
        return self.save(settings)

    def set_last_connected(self, timestamp_ms: int | None = None) -> Settings:
        return self.update(last_connected_at=timestamp_ms or models.now_ms())
