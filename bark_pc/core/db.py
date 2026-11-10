"""SQLite storage: schema migrations plus the repository used by the app.

One connection guarded by an `RLock` is shared by the receiver thread, the image
workers and the Tk thread (writes are rare and WAL keeps reads cheap).  All
timestamps are UTC epoch milliseconds.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from . import models
from .models import (
    BarkLink,
    DeviceIdentity,
    ImageCacheEntry,
    ImageState,
    LinkState,
    Notification,
    now_ms,
)

SCHEMA_VERSION = 1

_MIGRATION_V1 = """
CREATE TABLE IF NOT EXISTS device_identity (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    udid             TEXT NOT NULL DEFAULT '',
    serial           TEXT NOT NULL DEFAULT '',
    device_class     TEXT NOT NULL DEFAULT 'Windows',
    cert_fingerprint TEXT,
    state            TEXT NOT NULL DEFAULT 'absent'
                     CHECK (state IN ('absent','activating','active','invalid')),
    activated_at     INTEGER,
    last_used_at     INTEGER,
    last_error       TEXT
);

CREATE TABLE IF NOT EXISTS bark_link (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    server_url       TEXT NOT NULL DEFAULT '',
    device_key       TEXT NOT NULL DEFAULT '',
    push_topic       TEXT NOT NULL DEFAULT 'me.fin.bark',
    state            TEXT NOT NULL DEFAULT 'unlinked'
                     CHECK (state IN ('unlinked','registering','linked','stale','abandoned')),
    created_at       INTEGER,
    updated_at       INTEGER,
    last_verified_at INTEGER,
    last_error       TEXT,
    abandoned_at     INTEGER
);

CREATE TABLE IF NOT EXISTS image_cache (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url_hash     TEXT NOT NULL UNIQUE,
    url          TEXT NOT NULL,
    path         TEXT NOT NULL DEFAULT '',
    bytes        INTEGER NOT NULL DEFAULT 0,
    width        INTEGER NOT NULL DEFAULT 0,
    height       INTEGER NOT NULL DEFAULT 0,
    thumb_path   TEXT NOT NULL DEFAULT '',
    state        TEXT NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending','cached','unavailable','too_large','not_image')),
    fetched_at   INTEGER,
    last_used_at INTEGER
);

CREATE TABLE IF NOT EXISTS notification (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_id     TEXT,
    title         TEXT NOT NULL DEFAULT '',
    subtitle      TEXT,
    body          TEXT NOT NULL DEFAULT '',
    group_name    TEXT,
    url           TEXT,
    level         TEXT,
    image_url     TEXT,
    image_id      INTEGER REFERENCES image_cache(id) ON DELETE SET NULL,
    raw_json      TEXT,
    received_at   INTEGER NOT NULL,
    read_at       INTEGER,
    deleted_at    INTEGER,
    source_server TEXT NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_source_remote
    ON notification (source_server, remote_id) WHERE remote_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_notification_received
    ON notification (received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS ix_notification_unread
    ON notification (read_at) WHERE read_at IS NULL;
"""

MIGRATIONS: list[tuple[int, str]] = [(1, _MIGRATION_V1)]

IA_INSERT_IMAGE = """
INSERT INTO image_cache
    (url_hash, url, path, bytes, width, height, thumb_path, state, fetched_at, last_used_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (url_hash) DO UPDATE SET
    url=excluded.url,
    path=excluded.path,
    bytes=excluded.bytes,
    width=excluded.width,
    height=excluded.height,
    thumb_path=excluded.thumb_path,
    state=excluded.state,
    fetched_at=excluded.fetched_at
"""

_NOTIFICATION_COLUMNS = (
    "id, remote_id, title, subtitle, body, group_name, url, level, image_url, "
    "image_id, raw_json, received_at, read_at, deleted_at, source_server"
)

_IMAGE_COLUMNS = (
    "id, url_hash, url, path, bytes, width, height, thumb_path, state, fetched_at, last_used_at"
)


def _cap_utf8(text: str | None, limit: int) -> str:
    """Truncate *text* so its UTF-8 encoding fits in *limit* bytes."""
    if not text:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        remote_id=row["remote_id"],
        title=row["title"] or "",
        subtitle=row["subtitle"],
        body=row["body"] or "",
        group_name=row["group_name"],
        url=row["url"],
        level=row["level"],
        image_url=row["image_url"],
        image_id=row["image_id"],
        raw_json=row["raw_json"],
        received_at=row["received_at"],
        read_at=row["read_at"],
        deleted_at=row["deleted_at"],
        source_server=row["source_server"] or "",
    )


def _row_to_image(row: sqlite3.Row) -> ImageCacheEntry:
    return ImageCacheEntry(
        id=row["id"],
        url_hash=row["url_hash"],
        url=row["url"],
        path=row["path"] or "",
        bytes=row["bytes"] or 0,
        width=row["width"] or 0,
        height=row["height"] or 0,
        thumb_path=row["thumb_path"] or "",
        state=ImageState(row["state"]),
        fetched_at=row["fetched_at"],
        last_used_at=row["last_used_at"],
    )


class Database:
    """Owns the SQLite connection and exposes the repository API."""

    def __init__(
        self,
        path: str | Path,
        *,
        logger: logging.Logger | None = None,
        migrate: bool = True,
    ):
        self.path = Path(path)
        self.logger = logger or logging.getLogger("bark_pc.db")
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        if migrate:
            self.migrate()

    # -- lifecycle --------------------------------------------------------- #
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:  # pragma: no cover - defensive
                pass

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- migrations -------------------------------------------------------- #
    def migrate(self) -> int:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            applied = {
                row["version"]
                for row in self._conn.execute("SELECT version FROM schema_migrations")
            }
            version = max(applied) if applied else 0
            for target, script in MIGRATIONS:
                if target in applied:
                    continue
                self._conn.executescript(script)
                self._conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (target, now_ms()),
                )
                version = target
            self._conn.execute(f"PRAGMA user_version={version}")
            return version

    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("PRAGMA user_version").fetchone()
            return int(row[0]) if row else 0

    # -- notifications ----------------------------------------------------- #
    def insert_notification(self, notification: Notification) -> int:
        """Insert, ignoring duplicates by `(source_server, remote_id)`.

        Returns the id of the stored row (the pre-existing one on a duplicate).
        """
        payload = (
            notification.remote_id,
            _cap_utf8(notification.title, models.BODY_MAX_STORED_BYTES),
            notification.subtitle,
            _cap_utf8(notification.body, models.BODY_MAX_STORED_BYTES),
            notification.group_name,
            notification.url,
            notification.level,
            notification.image_url,
            notification.image_id,
            _cap_utf8(notification.raw_json, models.RAW_JSON_MAX_BYTES),
            int(notification.received_at or now_ms()),
            notification.read_at,
            notification.deleted_at,
            notification.source_server or "",
        )
        sql = """
        INSERT OR IGNORE INTO notification
            (remote_id, title, subtitle, body, group_name, url, level, image_url,
             image_id, raw_json, received_at, read_at, deleted_at, source_server)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._lock:
            cursor = self._conn.execute(sql, payload)
            if cursor.rowcount and cursor.lastrowid:
                notification.id = int(cursor.lastrowid)
                return int(cursor.lastrowid)
            if notification.remote_id is not None:
                row = self._conn.execute(
                    "SELECT id FROM notification WHERE source_server = ? AND remote_id = ?",
                    (notification.source_server or "", notification.remote_id),
                ).fetchone()
                if row:
                    notification.id = int(row["id"])
                    return int(row["id"])
            raise sqlite3.IntegrityError("notification could not be inserted")

    def list_notifications(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        include_deleted: bool = False,
    ) -> list[Notification]:
        clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM notification {clause} "
                "ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
                (int(limit), int(offset)),
            ).fetchall()
        return [_row_to_notification(row) for row in rows]

    def get_notification(self, notification_id: int) -> Notification | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM notification WHERE id = ?",
                (int(notification_id),),
            ).fetchone()
        return _row_to_notification(row) if row else None

    def find_by_remote_id(self, source_server: str, remote_id: str) -> Notification | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_NOTIFICATION_COLUMNS} FROM notification "
                "WHERE source_server = ? AND remote_id = ?",
                (source_server, remote_id),
            ).fetchone()
        return _row_to_notification(row) if row else None

    def count_notifications(self, *, include_deleted: bool = False) -> int:
        clause = "" if include_deleted else "WHERE deleted_at IS NULL"
        with self._lock:
            row = self._conn.execute(f"SELECT COUNT(*) AS n FROM notification {clause}").fetchone()
        return int(row["n"]) if row else 0

    def mark_read(self, notification_id: int | None = None) -> int:
        """Mark one notification (or all) as read. Returns rows changed."""
        timestamp = now_ms()
        with self._lock:
            if notification_id is None:
                cursor = self._conn.execute(
                    "UPDATE notification SET read_at = ? "
                    "WHERE read_at IS NULL AND deleted_at IS NULL",
                    (timestamp,),
                )
            else:
                cursor = self._conn.execute(
                    "UPDATE notification SET read_at = ? WHERE id = ? AND read_at IS NULL",
                    (timestamp, int(notification_id)),
                )
            return int(cursor.rowcount or 0)

    def mark_unread(self, notification_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE notification SET read_at = NULL WHERE id = ?",
                (int(notification_id),),
            )
            return int(cursor.rowcount or 0)

    def unread_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM notification "
                "WHERE read_at IS NULL AND deleted_at IS NULL"
            ).fetchone()
        return int(row["n"]) if row else 0

    def delete_notification(self, notification_id: int) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM notification WHERE id = ?", (int(notification_id),)
            )
            return bool(cursor.rowcount)

    def delete_by_remote_id(self, source_server: str, remote_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM notification WHERE source_server = ? AND remote_id = ?",
                (source_server, remote_id),
            )
            return bool(cursor.rowcount)

    def clear_history(self) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM notification")
            return int(cursor.rowcount or 0)

    def attach_image(self, notification_id: int, image_id: int | None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE notification SET image_id = ? WHERE id = ?",
                (image_id, int(notification_id)),
            )

    def set_image_for_url(self, image_url: str, image_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE notification SET image_id = ? WHERE image_url = ? AND image_id IS NULL",
                (image_id, image_url),
            )
            return int(cursor.rowcount or 0)

    def prune(
        self,
        retention_count: int = 500,
        retention_days: int = 30,
        *,
        now: int | None = None,
    ) -> dict[str, int]:
        """Hard-delete notifications beyond the age or count limits."""
        current = int(now if now is not None else now_ms())
        cutoff = current - int(retention_days) * 24 * 60 * 60 * 1000
        with self._lock:
            by_age = self._conn.execute(
                "DELETE FROM notification WHERE received_at < ?", (cutoff,)
            ).rowcount
            keep = max(0, int(retention_count))
            by_count = self._conn.execute(
                "DELETE FROM notification WHERE id NOT IN ("
                "  SELECT id FROM notification ORDER BY received_at DESC, id DESC LIMIT ?"
                ")",
                (keep,),
            ).rowcount
        return {"by_age": int(by_age or 0), "by_count": int(by_count or 0)}

    # -- device identity --------------------------------------------------- #
    def get_identity_meta(self) -> DeviceIdentity | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, udid, serial, device_class, cert_fingerprint, state, "
                "activated_at, last_used_at, last_error FROM device_identity WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return DeviceIdentity(
            id=row["id"],
            udid=row["udid"] or "",
            serial=row["serial"] or "",
            device_class=row["device_class"] or "Windows",
            cert_fingerprint=row["cert_fingerprint"],
            state=models.DeviceState(row["state"]),
            activated_at=row["activated_at"],
            last_used_at=row["last_used_at"],
            last_error=row["last_error"],
        )

    def upsert_identity_meta(self, identity: DeviceIdentity) -> DeviceIdentity:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO device_identity
                    (id, udid, serial, device_class, cert_fingerprint, state,
                     activated_at, last_used_at, last_error)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    udid=excluded.udid,
                    serial=excluded.serial,
                    device_class=excluded.device_class,
                    cert_fingerprint=excluded.cert_fingerprint,
                    state=excluded.state,
                    activated_at=excluded.activated_at,
                    last_used_at=excluded.last_used_at,
                    last_error=excluded.last_error
                """,
                (
                    identity.udid,
                    identity.serial,
                    identity.device_class,
                    identity.cert_fingerprint,
                    identity.state.value,
                    identity.activated_at,
                    identity.last_used_at,
                    identity.last_error,
                ),
            )
        return identity

    def set_identity_state(
        self,
        state: models.DeviceState,
        *,
        last_error: str | None = None,
        last_used_at: int | None = None,
    ) -> None:
        with self._lock:
            if last_used_at is not None:
                self._conn.execute(
                    "UPDATE device_identity SET state = ?, last_error = ?, last_used_at = ? "
                    "WHERE id = 1",
                    (state.value, last_error, last_used_at),
                )
            else:
                self._conn.execute(
                    "UPDATE device_identity SET state = ?, last_error = ? WHERE id = 1",
                    (state.value, last_error),
                )

    # -- Bark link --------------------------------------------------------- #
    def get_link(self) -> BarkLink | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, server_url, device_key, push_topic, state, created_at, "
                "updated_at, last_verified_at, last_error, abandoned_at "
                "FROM bark_link WHERE id = 1"
            ).fetchone()
        if not row:
            return None
        return BarkLink(
            id=row["id"],
            server_url=row["server_url"] or "",
            device_key=row["device_key"] or "",
            push_topic=row["push_topic"] or "me.fin.bark",
            state=LinkState(row["state"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_verified_at=row["last_verified_at"],
            last_error=row["last_error"],
            abandoned_at=row["abandoned_at"],
        )

    def upsert_link(self, link: BarkLink) -> BarkLink:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO bark_link
                    (id, server_url, device_key, push_topic, state, created_at,
                     updated_at, last_verified_at, last_error, abandoned_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    server_url=excluded.server_url,
                    device_key=excluded.device_key,
                    push_topic=excluded.push_topic,
                    state=excluded.state,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    last_verified_at=excluded.last_verified_at,
                    last_error=excluded.last_error,
                    abandoned_at=excluded.abandoned_at
                """,
                (
                    link.server_url,
                    link.device_key,
                    link.push_topic,
                    link.state.value,
                    link.created_at,
                    link.updated_at,
                    link.last_verified_at,
                    link.last_error,
                    link.abandoned_at,
                ),
            )
        return link

    def mark_link_state(
        self,
        state: LinkState,
        *,
        last_error: str | None = None,
        last_verified_at: int | None = None,
        abandoned_at: int | None = None,
    ) -> None:
        sets = ["state = ?", "updated_at = ?"]
        values: list = [state.value, now_ms()]
        if last_error is not None or state == LinkState.LINKED:
            sets.append("last_error = ?")
            values.append(last_error)
        if last_verified_at is not None:
            sets.append("last_verified_at = ?")
            values.append(last_verified_at)
        if abandoned_at is not None:
            sets.append("abandoned_at = ?")
            values.append(abandoned_at)
        values.append(1)
        with self._lock:
            self._conn.execute(f"UPDATE bark_link SET {', '.join(sets)} WHERE id = ?", values)

    # -- image cache ------------------------------------------------------- #
    def upsert_image(self, entry: ImageCacheEntry) -> ImageCacheEntry:
        with self._lock:
            cursor = self._conn.execute(
                IA_INSERT_IMAGE,
                (
                    entry.url_hash,
                    entry.url,
                    entry.path,
                    int(entry.bytes),
                    int(entry.width),
                    int(entry.height),
                    entry.thumb_path,
                    entry.state.value,
                    entry.fetched_at,
                    entry.last_used_at or entry.fetched_at or now_ms(),
                ),
            )
            if cursor.lastrowid:
                entry.id = int(cursor.lastrowid)
            if entry.id is None:
                row = self._conn.execute(
                    "SELECT id FROM image_cache WHERE url_hash = ?", (entry.url_hash,)
                ).fetchone()
                if row:
                    entry.id = int(row["id"])
        return entry

    def get_image(self, image_id: int) -> ImageCacheEntry | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_IMAGE_COLUMNS} FROM image_cache WHERE id = ?",
                (int(image_id),),
            ).fetchone()
        return _row_to_image(row) if row else None

    def get_image_by_hash(self, url_hash: str) -> ImageCacheEntry | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_IMAGE_COLUMNS} FROM image_cache WHERE url_hash = ?",
                (url_hash,),
            ).fetchone()
        return _row_to_image(row) if row else None

    def get_image_by_url(self, url: str) -> ImageCacheEntry | None:
        return self.get_image_by_hash(image_url_hash(url))

    def touch_image(self, image_id: int, *, timestamp: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE image_cache SET last_used_at = ? WHERE id = ?",
                (int(timestamp or now_ms()), int(image_id)),
            )

    def update_image_state(
        self, image_id: int, state: ImageState, *, url: str | None = None
    ) -> None:
        with self._lock:
            if url is not None:
                self._conn.execute(
                    "UPDATE image_cache SET state = ?, url = ? WHERE id = ?",
                    (state.value, url, int(image_id)),
                )
            else:
                self._conn.execute(
                    "UPDATE image_cache SET state = ? WHERE id = ?",
                    (state.value, int(image_id)),
                )

    def delete_image(self, image_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM image_cache WHERE id = ?", (int(image_id),))

    def image_cache_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) AS n FROM image_cache WHERE state = 'cached'"
            ).fetchone()
        return int(row["n"]) if row else 0

    def list_images_lru(
        self, *, state: ImageState | None = ImageState.CACHED
    ) -> list[ImageCacheEntry]:
        clause = "" if state is None else "WHERE state = ?"
        params: tuple = () if state is None else (state.value,)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_IMAGE_COLUMNS} FROM image_cache {clause} "
                "ORDER BY COALESCE(last_used_at, 0) ASC, id ASC",
                params,
            ).fetchall()
        return [_row_to_image(row) for row in rows]

    def referenced_image_ids(self) -> set[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT image_id FROM notification WHERE image_id IS NOT NULL"
            ).fetchall()
        return {int(row["image_id"]) for row in rows}

    def referenced_image_urls(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT image_url FROM notification "
                "WHERE image_url IS NOT NULL AND image_url != ''"
            ).fetchall()
        return {str(row["image_url"]) for row in rows}

    # -- maintenance helpers ----------------------------------------------- #
    def stats(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT (SELECT COUNT(*) FROM notification) AS notifications, "
                "(SELECT COUNT(*) FROM notification WHERE read_at IS NULL "
                " AND deleted_at IS NULL) AS unread, "
                "(SELECT COUNT(*) FROM image_cache) AS images"
            ).fetchone()
        return {key: int(row[key]) for key in ("notifications", "unread", "images")}


def image_url_hash(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


class RecentPayloadFilter:
    """Drop repeats of the same payload when no `remote_id` is present.

    The contract allows de-duplication on the raw payload hash within a five
    second window.  State is in-memory: a redelivery after a restart is rare and
    would simply appear as a new notification.
    """

    def __init__(self, window_ms: int = 5_000, *, clock=time.monotonic):
        self.window_ms = window_ms
        self._clock = clock
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, source_server: str, raw: bytes) -> bool:
        digest = hashlib.sha256(raw).hexdigest()
        key = (source_server or "", digest)
        now = self._clock() * 1000.0
        with self._lock:
            for stale_key, seen_at in list(self._seen.items()):
                if now - seen_at > self.window_ms:
                    del self._seen[stale_key]
            previous = self._seen.get(key)
            self._seen[key] = now
            return previous is not None and (now - previous) <= self.window_ms

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()


def iter_notification_batches(
    notifications: Iterable[Notification], size: int = 100
) -> Iterator[list[Notification]]:
    batch: list[Notification] = []
    for notification in notifications:
        batch.append(notification)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
