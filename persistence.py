"""SQLite persistence for TomatoIQ.

WAL mode and short explicit transactions make this safe for concurrent API workers,
unlike the former read-modify-write JSON history file.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TomatoRepository:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS scan_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL UNIQUE,
                    total_tomatoes INTEGER NOT NULL,
                    ready_now INTEGER NOT NULL,
                    disease_suspect_count INTEGER NOT NULL,
                    counts_by_class TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    tomato_id INTEGER,
                    severity TEXT NOT NULL CHECK(severity IN ('info','warning','critical')),
                    category TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    resolved_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON scan_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at DESC);
            """)

    def append_snapshot(self, state: dict[str, Any]) -> bool:
        timestamp = state.get("updated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connect() as db:
            last = db.execute("SELECT timestamp FROM scan_snapshots ORDER BY id DESC LIMIT 1").fetchone()
            if last:
                try:
                    if datetime.fromisoformat(timestamp).timestamp() - datetime.fromisoformat(last["timestamp"]).timestamp() < 60:
                        return False
                except ValueError:
                    pass
            try:
                db.execute("""INSERT INTO scan_snapshots(timestamp,total_tomatoes,ready_now,disease_suspect_count,counts_by_class)
                              VALUES(?,?,?,?,?)""", (timestamp, int(state.get("total_tomatoes", 0)), int(state.get("ready_now", 0)), int(state.get("disease_suspect_count", 0)), json.dumps(state.get("counts_by_class", {}))))
                return True
            except sqlite3.IntegrityError:
                return False

    def snapshots_since(self, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM scan_snapshots WHERE timestamp >= ? ORDER BY timestamp", (cutoff.isoformat(),)).fetchall()
        return [{"timestamp": row["timestamp"], "total_tomatoes": row["total_tomatoes"], "ready_now": row["ready_now"], "disease_suspect_count": row["disease_suspect_count"], "counts_by_class": json.loads(row["counts_by_class"])} for row in rows]

    def create_alert(self, *, tomato_id: int | None, severity: str, category: str, message: str, metadata: dict[str, Any] | None = None) -> int:
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            cursor = db.execute("INSERT INTO alerts(created_at,tomato_id,severity,category,message,metadata) VALUES(?,?,?,?,?,?)", (created_at, tomato_id, severity, category, message, json.dumps(metadata or {})))
            return int(cursor.lastrowid)

    def ensure_open_alert(self, *, tomato_id: int | None, severity: str, category: str, message: str, metadata: dict[str, Any] | None = None) -> bool:
        """Create one open alert per tomato/category until an operator resolves it."""
        with self._connect() as db:
            row = db.execute("SELECT id FROM alerts WHERE tomato_id IS ? AND category=? AND resolved_at IS NULL", (tomato_id, category)).fetchone()
            if row:
                return False
        self.create_alert(tomato_id=tomato_id, severity=severity, category=category, message=message, metadata=metadata)
        return True

    def list_alerts(self, limit: int = 100, include_resolved: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts" + ("" if include_resolved else " WHERE resolved_at IS NULL") + " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as db:
            rows = db.execute(sql, (limit,)).fetchall()
        return [dict(row) | {"metadata": json.loads(row["metadata"])} for row in rows]

    def resolve_alert(self, alert_id: int) -> bool:
        with self._connect() as db:
            result = db.execute("UPDATE alerts SET resolved_at=? WHERE id=? AND resolved_at IS NULL", (datetime.now(timezone.utc).isoformat(timespec="seconds"), alert_id))
            return result.rowcount == 1
