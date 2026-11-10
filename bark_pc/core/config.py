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
from urllib.parse import urlsplit

from ..tts.base import DEFAULT_TTS_PROVIDER, TTS_PROVIDERS
from . import models

APP_DIR_ENV = "BARK_PC_HOME"
DATA_DIR_ENV = "BARK_PC_DATA"
APP_DATA_DIRNAME = "BarkDesktopClient"
DEFAULT_SERVER_URL = "https://api.day.app"
DEFAULT_PUSH_TOPIC = "me.fin.bark"
SETTINGS_FILENAME = "settings.json"

LOG_LEVELS = ("debug", "info", "warning", "error")


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
    tts_rate: int = 0
    tts_volume: int = 100
    tts_max_chars: int = 300
    silence_until: int | None = None
    autostart_enabled: bool = False
    autostart_path: str | None = None
    launch_at_login_configured_at: int | None = None
    last_connected_at: int | None = None
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
        return settings

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
            if name == "server_url":
                return validate_server_url(str(raw))
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


def validate_server_url(raw: str) -> str:
    """Normalize a Bark server URL or raise ValueError explaining the problem."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("server address is empty")
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
