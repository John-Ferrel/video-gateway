"""SQLite persistence for jobs, uploads, batches and the usage ledger.

The schema is deliberately small. A single writer (the dispatcher) and short
read/write transactions keep SQLite more than fast enough for a personal video
gateway. WAL mode is enabled so HTTP reads never block on dispatcher writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

ACTIVE_STATUSES = ("queued", "submitting", "processing", "downloading")
TERMINAL_STATUSES = ("completed", "failed", "cancelled", "expired")

VIDEO_COLUMNS = {
    "object",
    "batch_id",
    "model",
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "seconds",
    "fps",
    "seed",
    "with_audio",
    "input_image_path",
    "input_image_mime",
    "input_image_bytes",
    "status",
    "progress",
    "stage",
    "created_at",
    "queued_at",
    "started_at",
    "completed_at",
    "modal_call_id",
    "output_path",
    "thumb_path",
    "output_bytes",
    "duration_s",
    "cost_usd",
    "reserved_usd",
    "gpu_seconds",
    "cpu_seconds",
    "mem_gib_seconds",
    "model_load_s",
    "inference_s",
    "encode_s",
    "peak_vram_mb",
    "cold_start",
    "error",
    "meta_json",
    "cancel_requested",
    "deleted_at",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._init_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._init_lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY,
                    object TEXT NOT NULL DEFAULT 'video',
                    batch_id TEXT,
                    model TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    negative_prompt TEXT,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    seconds REAL NOT NULL,
                    fps INTEGER NOT NULL,
                    seed INTEGER,
                    with_audio INTEGER NOT NULL DEFAULT 1,
                    input_image_path TEXT,
                    input_image_mime TEXT,
                    input_image_bytes INTEGER,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    stage TEXT,
                    created_at TEXT NOT NULL,
                    queued_at TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    modal_call_id TEXT,
                    output_path TEXT,
                    thumb_path TEXT,
                    output_bytes INTEGER,
                    duration_s REAL,
                    cost_usd REAL NOT NULL DEFAULT 0,
                    reserved_usd REAL NOT NULL DEFAULT 0,
                    gpu_seconds REAL,
                    cpu_seconds REAL,
                    mem_gib_seconds REAL,
                    model_load_s REAL,
                    inference_s REAL,
                    encode_s REAL,
                    peak_vram_mb REAL,
                    cold_start INTEGER,
                    error TEXT,
                    meta_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_videos_status_created
                    ON videos(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_videos_created
                    ON videos(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_videos_batch
                    ON videos(batch_id, created_at);

                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    label TEXT,
                    meta_json TEXT
                );

                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    video_id TEXT,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime TEXT,
                    bytes INTEGER,
                    sha256 TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger (
                    id TEXT PRIMARY KEY,
                    ts TEXT NOT NULL,
                    video_id TEXT,
                    kind TEXT NOT NULL,
                    amount_usd REAL NOT NULL,
                    detail TEXT,
                    UNIQUE(video_id, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);
                CREATE INDEX IF NOT EXISTS idx_ledger_video ON ledger(video_id);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    video_id TEXT,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
                """
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------------------ videos
    def create_video(self, **fields: Any) -> dict:
        video_id = fields.pop("id", None) or "video_" + uuid.uuid4().hex
        now = fields.pop("created_at", None) or iso()
        fields.setdefault("object", "video")
        fields.setdefault("status", "queued")
        fields.setdefault("progress", 0)
        fields.setdefault("queued_at", now)
        fields.setdefault("with_audio", 1)
        unknown = set(fields) - VIDEO_COLUMNS
        if unknown:
            raise ValueError(f"Unknown video columns: {sorted(unknown)}")
        fields["id"] = video_id
        fields["created_at"] = now
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with self._connection() as connection:
            connection.execute(
                f"INSERT INTO videos ({columns}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
        return self.get_video(video_id) or {}

    def update_video(self, video_id: str, **fields: Any) -> dict | None:
        unknown = set(fields) - VIDEO_COLUMNS
        if unknown:
            raise ValueError(f"Unknown video columns: {sorted(unknown)}")
        if not fields:
            return self.get_video(video_id)
        assignments = ", ".join(f"{column} = ?" for column in fields)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE videos SET {assignments} WHERE id = ?",
                (*fields.values(), video_id),
            )
        return self.get_video(video_id)

    def get_video(self, video_id: str, include_deleted: bool = False) -> dict | None:
        query = "SELECT * FROM videos WHERE id = ?"
        if not include_deleted:
            query += " AND deleted_at IS NULL"
        with self._connection() as connection:
            row = connection.execute(query, (video_id,)).fetchone()
        return dict(row) if row else None

    def list_videos(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        batch_id: str | None = None,
        since: str | None = None,
        include_deleted: bool = False,
    ) -> list[dict]:
        clauses = []
        params: list[Any] = []
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if status:
            clauses.append("status = ?")
            params.append(status)
        if batch_id:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.extend([max(1, min(limit, 200)), max(0, offset)])
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM videos{where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_videos(self, *, status: str | None = None, since: str | None = None) -> int:
        clauses = ["deleted_at IS NULL"]
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE " + " AND ".join(clauses),
                tuple(params),
            ).fetchone()
        return int(row["n"])

    def active_jobs(self) -> list[dict]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM videos WHERE deleted_at IS NULL AND status IN ({placeholders}) "
                "ORDER BY created_at",
                ACTIVE_STATUSES,
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_next_queued(self) -> dict | None:
        """Atomically move the oldest queued job to `submitting`.

        A single dispatcher is expected, but the transaction makes this safe if
        a second process is accidentally started.
        """
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM videos WHERE deleted_at IS NULL AND status = 'queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            connection.execute(
                "UPDATE videos SET status = 'submitting', stage = 'submitting', started_at = ? "
                "WHERE id = ?",
                (iso(), row["id"]),
            )
            claimed = connection.execute("SELECT * FROM videos WHERE id = ?", (row["id"],)).fetchone()
        return dict(claimed) if claimed else None

    def queue_position(self, video_id: str) -> int | None:
        with self._connection() as connection:
            target = connection.execute(
                "SELECT created_at, status FROM videos WHERE id = ?", (video_id,)
            ).fetchone()
            if not target or target["status"] != "queued":
                return None
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE deleted_at IS NULL AND status = 'queued' "
                "AND created_at < ?",
                (target["created_at"],),
            ).fetchone()
        return int(row["n"]) + 1

    def queued_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE deleted_at IS NULL AND status = 'queued'"
            ).fetchone()
        return int(row["n"])

    def mark_cancelled(self, video_id: str, reason: str = "cancelled by user") -> dict | None:
        now = iso()
        return self.update_video(
            video_id,
            status="cancelled",
            stage="cancelled",
            completed_at=now,
            reserved_usd=0.0,
            error=reason,
        )

    # ------------------------------------------------------------------ batches
    def create_batch(self, *, model: str, total: int, label: str | None = None) -> dict:
        batch_id = "batch_" + uuid.uuid4().hex
        now = iso()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO batches (id, created_at, model, total, label) VALUES (?, ?, ?, ?, ?)",
                (batch_id, now, model, total, label),
            )
        return self.get_batch(batch_id) or {}

    def get_batch(self, batch_id: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if not row:
                return None
            counts = connection.execute(
                "SELECT status, COUNT(*) AS n FROM videos WHERE batch_id = ? AND deleted_at IS NULL "
                "GROUP BY status",
                (batch_id,),
            ).fetchall()
        result = dict(row)
        result["status_counts"] = {item["status"]: int(item["n"]) for item in counts}
        result["completed"] = sum(
            result["status_counts"].get(status, 0) for status in ("completed", "failed", "cancelled")
        )
        return result

    def list_batches(self, limit: int = 20) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self.get_batch(row["id"]) or dict(row) for row in rows]

    # ------------------------------------------------------------------ uploads
    def add_upload(
        self,
        *,
        video_id: str | None,
        kind: str,
        path: str,
        mime: str | None,
        size: int | None,
        sha256: str | None = None,
    ) -> str:
        upload_id = "upload_" + uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO uploads (id, video_id, kind, path, mime, bytes, sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (upload_id, video_id, kind, path, mime, size, sha256, iso()),
            )
        return upload_id

    # ------------------------------------------------------------------ ledger
    def add_ledger(
        self,
        *,
        video_id: str | None,
        kind: str,
        amount_usd: float,
        detail: str | None = None,
        ts: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO ledger (id, ts, video_id, kind, amount_usd, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"ledger_{uuid.uuid4().hex}",
                    ts or iso(),
                    video_id,
                    kind,
                    float(amount_usd),
                    detail,
                ),
            )

    def ledger_total(self, since: str | None = None) -> float:
        query = "SELECT COALESCE(SUM(amount_usd), 0) AS total FROM ledger"
        params: tuple[Any, ...] = ()
        if since:
            query += " WHERE ts >= ?"
            params = (since,)
        with self._connection() as connection:
            row = connection.execute(query, params).fetchone()
        return float(row["total"])

    def ledger_breakdown(self, since: str | None = None) -> list[dict]:
        query = (
            "SELECT kind, COUNT(*) AS entries, COALESCE(SUM(amount_usd), 0) AS total "
            "FROM ledger"
        )
        params: tuple[Any, ...] = ()
        if since:
            query += " WHERE ts >= ?"
            params = (since,)
        query += " GROUP BY kind ORDER BY total DESC"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def daily_spend(self, since: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT substr(ts, 1, 10) AS day, COALESCE(SUM(amount_usd), 0) AS total "
                "FROM ledger WHERE ts >= ? GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ settings
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, str(value), iso()),
            )

    def get_json_setting(self, key: str, default: Any = None) -> Any:
        raw = self.get_setting(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json_setting(self, key: str, value: Any) -> None:
        self.set_setting(key, json.dumps(value, separators=(",", ":")))

    # ------------------------------------------------------------------ events
    def add_event(
        self,
        *,
        video_id: str | None,
        level: str,
        message: str,
        detail: Any = None,
    ) -> None:
        try:
            detail_text = json.dumps(detail, default=str)[:8000] if detail is not None else None
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO events (ts, video_id, level, message, detail) VALUES (?, ?, ?, ?, ?)",
                    (iso(), video_id, level, message[:1000], detail_text),
                )
        except Exception:
            # Event logging must never break a job.
            pass

    def recent_events(self, limit: int = 50) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ cleanup
    def expired_videos(self, before: str, limit: int = 200) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM videos WHERE deleted_at IS NULL AND completed_at IS NOT NULL "
                "AND completed_at < ? ORDER BY completed_at LIMIT ?",
                (before, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def soft_delete(self, video_id: str) -> None:
        self.update_video(video_id, deleted_at=iso())

    def totals(self) -> dict:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(cost_usd), 0) AS cost, "
                "COALESCE(SUM(output_bytes), 0) AS bytes FROM videos WHERE deleted_at IS NULL"
            ).fetchone()
            completed = connection.execute(
                "SELECT COUNT(*) AS n FROM videos WHERE deleted_at IS NULL AND status = 'completed'"
            ).fetchone()
        return {"videos": int(row["n"]), "completed": int(completed["n"]),
                "cost_usd": float(row["cost"]), "output_bytes": int(row["bytes"])}
