"""SQLite catalog for symbols and per-day download status."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

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


def set_day_status(
    conn: sqlite3.Connection,
    symbol: str,
    day: str,
    status: str,
    *,
    ticks: int = 0,
    bars_1s: int = 0,
    raw_path: str | None = None,
    parquet_path: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO download_days (
          symbol, day, status, ticks, bars_1s, raw_path, parquet_path, error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, day) DO UPDATE SET
          status = excluded.status,
          ticks = excluded.ticks,
          bars_1s = excluded.bars_1s,
          raw_path = COALESCE(excluded.raw_path, download_days.raw_path),
          parquet_path = COALESCE(excluded.parquet_path, download_days.parquet_path),
          error = excluded.error,
          updated_at = excluded.updated_at
        """,
        (symbol, day, status, ticks, bars_1s, raw_path, parquet_path, error, utc_now()),
    )
    conn.commit()
