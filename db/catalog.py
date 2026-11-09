"""SQLite catalog for days, hours, and multi-day jobs."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

JOB_STATES = ("QUEUED", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")

_FILE_ID_COLUMNS = (
    "tick_file_id",
    "bars_1s_file_id",
    "metadata_file_id",
    "storage_message_ids",
)


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate_file_id_columns(conn)
    return conn


def _migrate_file_id_columns(conn: sqlite3.Connection) -> None:
    """Add Telegram file_id columns on existing databases."""
    cur = conn.execute("PRAGMA table_info(download_days)")
    existing = {row[1] for row in cur.fetchall()}
    for col in _FILE_ID_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE download_days ADD COLUMN {col} TEXT")
    conn.commit()


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
          tick_file_path, bars_1s_file_path, metadata_path,
          tick_file_id, bars_1s_file_id, metadata_file_id, storage_message_ids,
          error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, provider, day) DO UPDATE SET
          status = excluded.status,
          tick_status = excluded.tick_status,
          bars_1s_status = excluded.bars_1s_status,
          tick_count = excluded.tick_count,
          bars_1s_count = excluded.bars_1s_count,
          successful_hours = excluded.successful_hours,
          empty_hours = excluded.empty_hours,
          failed_hours = excluded.failed_hours,
          tick_file_path = COALESCE(excluded.tick_file_path, download_days.tick_file_path),
          bars_1s_file_path = COALESCE(excluded.bars_1s_file_path, download_days.bars_1s_file_path),
          metadata_path = COALESCE(excluded.metadata_path, download_days.metadata_path),
          tick_file_id = COALESCE(excluded.tick_file_id, download_days.tick_file_id),
          bars_1s_file_id = COALESCE(excluded.bars_1s_file_id, download_days.bars_1s_file_id),
          metadata_file_id = COALESCE(excluded.metadata_file_id, download_days.metadata_file_id),
          storage_message_ids = COALESCE(excluded.storage_message_ids, download_days.storage_message_ids),
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
            row.get("tick_file_id"),
            row.get("bars_1s_file_id"),
            row.get("metadata_file_id"),
            row.get("storage_message_ids"),
            row.get("error"),
            utc_now(),
        ),
    )
    conn.commit()


def set_day_file_ids(
    conn: sqlite3.Connection,
    symbol: str,
    day: str,
    *,
    provider: str = "dukascopy",
    tick_file_id: str | None = None,
    bars_1s_file_id: str | None = None,
    metadata_file_id: str | None = None,
    storage_message_ids: str | None = None,
) -> None:
    """Update only the Telegram file_id fields after a successful channel upload."""
    conn.execute(
        """
        UPDATE download_days SET
          tick_file_id = COALESCE(?, tick_file_id),
          bars_1s_file_id = COALESCE(?, bars_1s_file_id),
          metadata_file_id = COALESCE(?, metadata_file_id),
          storage_message_ids = COALESCE(?, storage_message_ids),
          updated_at = ?
        WHERE symbol = ? AND provider = ? AND day = ?
        """,
        (
            tick_file_id,
            bars_1s_file_id,
            metadata_file_id,
            storage_message_ids,
            utc_now(),
            symbol,
            provider,
            day,
        ),
    )
    conn.commit()


def get_day(
    conn: sqlite3.Connection, symbol: str, day: str, provider: str = "dukascopy"
) -> dict[str, Any] | None:
    cur = conn.execute(
        "SELECT * FROM download_days WHERE symbol = ? AND provider = ? AND day = ?",
        (symbol, provider, day),
    )
    r = cur.fetchone()
    return dict(r) if r else None


def day_is_fully_verified(
    conn: sqlite3.Connection, symbol: str, day: str, provider: str = "dukascopy"
) -> bool:
    row = get_day(conn, symbol, day, provider)
    if not row:
        return False
    return (
        row["status"] == "SUCCESS"
        and row.get("tick_status") == "SUCCESS"
        and row.get("bars_1s_status") == "SUCCESS"
    )


def upsert_hour(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO download_hours (
          symbol, provider, day, hour, status, tick_count, http_status, attempts, error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, provider, day, hour) DO UPDATE SET
          status = excluded.status,
          tick_count = excluded.tick_count,
          http_status = excluded.http_status,
          attempts = excluded.attempts,
          error = excluded.error,
          updated_at = excluded.updated_at
        """,
        (
            row["symbol"],
            row.get("provider", "dukascopy"),
            row["day"],
            int(row["hour"]),
            row["status"],
            int(row.get("tick_count") or 0),
            row.get("http_status"),
            int(row.get("attempts") or 0),
            row.get("error"),
            utc_now(),
        ),
    )
    conn.commit()


def get_hour_statuses(
    conn: sqlite3.Connection, symbol: str, day: str, provider: str = "dukascopy"
) -> dict[int, str]:
    cur = conn.execute(
        "SELECT hour, status FROM download_hours WHERE symbol = ? AND provider = ? AND day = ?",
        (symbol, provider, day),
    )
    return {int(r["hour"]): r["status"] for r in cur.fetchall()}


def hours_to_fetch(
    conn: sqlite3.Connection, symbol: str, day: str, provider: str = "dukascopy"
) -> list[int]:
    """Hours that still need download (not SUCCESS or EXPECTED_EMPTY)."""
    known = get_hour_statuses(conn, symbol, day, provider)
    out = []
    for h in range(24):
        st = known.get(h)
        if st in ("SUCCESS", "EXPECTED_EMPTY"):
            continue
        out.append(h)
    return out


def create_job(
    conn: sqlite3.Connection,
    *,
    symbols: list[str],
    start_day: str,
    end_day: str,
    provider: str = "dukascopy",
    kind: str = "range",
    max_retries: int = 3,
) -> int:
    symbols_json = json.dumps(sorted(set(symbols)))
    # Prevent overlapping active jobs for same range+symbols
    cur = conn.execute(
        """
        SELECT id FROM download_jobs
        WHERE symbols_json = ? AND start_day = ? AND end_day = ? AND provider = ?
          AND status IN ('QUEUED', 'RUNNING', 'PAUSED')
        """,
        (symbols_json, start_day, end_day, provider),
    )
    existing = cur.fetchone()
    if existing:
        return int(existing["id"])

    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO download_jobs (
          kind, symbols_json, start_day, end_day, provider, status,
          progress_json, retry_count, max_retries, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, 0, ?, ?, ?)
        """,
        (
            kind,
            symbols_json,
            start_day,
            end_day,
            provider,
            json.dumps({"done_days": 0, "total_days": 0, "skipped": 0, "failed": 0}),
            max_retries,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM download_jobs WHERE id = ?", (job_id,))
    r = cur.fetchone()
    return dict(r) if r else None


def update_job(conn: sqlite3.Connection, job_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = utc_now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE download_jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
    conn.commit()


def set_job_status(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    if status not in JOB_STATES:
        raise ValueError(f"invalid job status: {status}")
    update_job(conn, job_id, status=status, error=error)


def list_active_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM download_jobs WHERE status IN ('QUEUED', 'RUNNING', 'PAUSED') ORDER BY id"
    )
    return [dict(r) for r in cur.fetchall()]


def upsert_job_day(
    conn: sqlite3.Connection, job_id: int, symbol: str, day: str, status: str, error: str | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO job_day_results (job_id, symbol, day, status, error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id, symbol, day) DO UPDATE SET
          status = excluded.status,
          error = excluded.error,
          updated_at = excluded.updated_at
        """,
        (job_id, symbol, day, status, error, utc_now()),
    )
    conn.commit()


def job_day_results(conn: sqlite3.Connection, job_id: int) -> list[dict[str, Any]]:
    cur = conn.execute(
        "SELECT * FROM job_day_results WHERE job_id = ? ORDER BY symbol, day", (job_id,)
    )
    return [dict(r) for r in cur.fetchall()]
