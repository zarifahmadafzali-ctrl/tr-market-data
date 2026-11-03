"""SQLite catalog for download day status."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert_symbol(conn: sqlite3.Connection, symbol: str, dukascopy_id: str, description: str = "") -> None:
    conn.execute(
        """
        INSERT INTO symbols (symbol, dukascopy_id, description, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
          dukascopy_id = excluded.dukascopy_id,
          description = excluded.description
        """,
        (symbol, dukascopy_id, description, utc_now()),
    )
    conn.commit()


def upsert_day(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO download_days (
          symbol, provider, day, status, tick_status, bars_1s_status,
          tick_count, bars_1s_count, successful_hours, empty_hours, failed_hours,
          tick_file_path, bars_1s_file_path, metadata_path, error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, provider, day) DO UPDATE SET
          status = excluded.status,
          tick_status = excluded.tick_status,
          bars_1s_status = excluded.bars_1s_status,
          tick_count = excluded.tick_count,
          bars_1s_count = excluded.bars_1s_count,
          successful_hours = excluded.successful_hours,
          empty_hours = excluded.empty_hours,
          failed_hours = excluded.failed_hours,
          tick_file_path = excluded.tick_file_path,
          bars_1s_file_path = excluded.bars_1s_file_path,
          metadata_path = excluded.metadata_path,
          error = excluded.error,
          updated_at = excluded.updated_at
        """,
        (
            row["symbol"],
            row.get("provider", "dukascopy"),
            row["day"],
            row["status"],
            row.get("tick_status"),
            row.get("bars_1s_status"),
            int(row.get("tick_count") or 0),
            int(row.get("bars_1s_count") or 0),
            int(row.get("successful_hours") or 0),
            int(row.get("empty_hours") or 0),
            int(row.get("failed_hours") or 0),
            row.get("tick_file_path"),
            row.get("bars_1s_file_path"),
            row.get("metadata_path"),
            row.get("error"),
            utc_now(),
        ),
    )
    conn.commit()


def get_day(conn: sqlite3.Connection, symbol: str, day: str, provider: str = "dukascopy") -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM download_days WHERE symbol = ? AND provider = ? AND day = ?",
        (symbol, provider, day),
    )
    r = cur.fetchone()
    return dict(r) if r else None
