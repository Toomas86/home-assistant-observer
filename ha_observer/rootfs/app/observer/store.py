"""Persistent, sanitized system-log storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .redaction import redact


def _as_epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


class EventStore:
    """A tiny SQLite store; every payload is redacted before disk write."""

    def __init__(self, path: str, retention_days: int = 30) -> None:
        self.path = Path(path)
        self.retention_days = max(1, min(retention_days, 365))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS system_log (
                    fingerprint TEXT PRIMARY KEY,
                    occurred REAL NOT NULL,
                    first_occurred REAL NOT NULL,
                    level TEXT NOT NULL,
                    name TEXT NOT NULL,
                    message_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    exception TEXT NOT NULL,
                    count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_system_log_occurred ON system_log(occurred DESC)"
            )
            connection.commit()
        self._prune_sync()

    async def upsert(self, entry: dict[str, Any]) -> None:
        await asyncio.to_thread(self._upsert_sync, entry)

    def _upsert_sync(self, entry: dict[str, Any]) -> None:
        safe = redact(entry)
        occurred = _as_epoch(safe.get("timestamp") or safe.get("time_fired"))
        first_occurred = _as_epoch(safe.get("first_occurred") or occurred)
        name = str(safe.get("name", "unknown"))
        source = safe.get("source", "")
        message = safe.get("message", "")
        exception = str(safe.get("exception", ""))
        fingerprint_material = json.dumps(
            [name, source, first_occurred], sort_keys=True, separators=(",", ":")
        )
        fingerprint = hashlib.sha256(fingerprint_material.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO system_log (
                    fingerprint, occurred, first_occurred, level, name,
                    message_json, source_json, exception, count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    occurred=excluded.occurred,
                    level=excluded.level,
                    message_json=excluded.message_json,
                    exception=excluded.exception,
                    count=MAX(system_log.count, excluded.count)
                """,
                (
                    fingerprint,
                    occurred,
                    first_occurred,
                    str(safe.get("level", "WARNING")).upper(),
                    name,
                    json.dumps(message, ensure_ascii=False),
                    json.dumps(source, ensure_ascii=False),
                    exception,
                    int(safe.get("count", 1) or 1),
                ),
            )
            connection.commit()

    async def prune(self) -> None:
        await asyncio.to_thread(self._prune_sync)

    def _prune_sync(self) -> None:
        cutoff = time.time() - self.retention_days * 86_400
        with self._connect() as connection:
            connection.execute("DELETE FROM system_log WHERE occurred < ?", (cutoff,))
            connection.commit()

    async def query(
        self,
        *,
        limit: int = 50,
        level: str | None = None,
        integration: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._query_sync,
            max(1, min(limit, 200)),
            level,
            integration,
            search,
        )

    def _query_sync(
        self,
        limit: int,
        level: str | None,
        integration: str | None,
        search: str | None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if level:
            clauses.append("level = ?")
            params.append(level.upper())
        if integration:
            clauses.append("LOWER(name) LIKE ?")
            params.append(f"%{integration.lower()}%")
        if search:
            clauses.append(
                "(LOWER(name) LIKE ? OR LOWER(message_json) LIKE ? OR LOWER(exception) LIKE ?)"
            )
            needle = f"%{search.lower()}%"
            params.extend([needle, needle, needle])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM system_log" + where + " ORDER BY occurred DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "timestamp": datetime.fromtimestamp(row["occurred"], UTC).isoformat(),
                "first_occurred": datetime.fromtimestamp(row["first_occurred"], UTC).isoformat(),
                "level": row["level"],
                "name": row["name"],
                "message": json.loads(row["message_json"]),
                "source": json.loads(row["source_json"]),
                "exception": row["exception"],
                "count": row["count"],
            }
            for row in rows
        ]
