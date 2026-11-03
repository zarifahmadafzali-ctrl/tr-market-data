"""Multi-day download job runner with pause/resume and restart recovery."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from db.catalog import (
    connect,
    create_job,
    day_is_fully_verified,
    get_job,
    job_day_results,
    list_active_jobs,
    set_job_status,
    update_job,
    upsert_job_day,
)
from downloader.symbols import normalize_symbol
from processor.pipeline import run_day_pipeline

log = logging.getLogger(__name__)

DayRunner = Callable[..., dict[str, Any]]


def daterange(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end_day must be >= start_day")
    days = []
    d = start
    while d <= end:
        days.append(d)
        d += timedelta(days=1)
    return days


def enqueue_range_job(
    db_path: Path,
    *,
    symbols: list[str],
    start_day: date,
    end_day: date,
    provider: str = "dukascopy",
) -> int:
    conn = connect(db_path)
    syms = [normalize_symbol(s) for s in symbols]
    job_id = create_job(
        conn,
        symbols=syms,
        start_day=start_day.isoformat(),
        end_day=end_day.isoformat(),
        provider=provider,
        kind="range",
    )
    total = len(syms) * len(daterange(start_day, end_day))
    update_job(
        conn,
        job_id,
        progress_json=json.dumps(
            {"done_days": 0, "total_days": total, "skipped": 0, "failed": 0, "success": 0}
        ),
    )
    return job_id


def pause_job(db_path: Path, job_id: int) -> None:
    conn = connect(db_path)
    job = get_job(conn, job_id)
    if not job:
        raise KeyError(f"job {job_id} not found")
    if job["status"] not in ("QUEUED", "RUNNING"):
        return
    set_job_status(conn, job_id, "PAUSED")


def resume_job(db_path: Path, job_id: int) -> None:
    conn = connect(db_path)
    job = get_job(conn, job_id)
    if not job:
        raise KeyError(f"job {job_id} not found")
    if job["status"] in ("PAUSED", "FAILED", "QUEUED"):
        set_job_status(conn, job_id, "QUEUED", error=None)


def cancel_job(db_path: Path, job_id: int) -> None:
    conn = connect(db_path)
    set_job_status(conn, job_id, "CANCELLED")


def run_job(
    db_path: Path,
    job_id: int,
    *,
    data_root: Path,
    day_runner: DayRunner | None = None,
) -> dict[str, Any]:
    """Execute a QUEUED/PAUSED/RUNNING job until COMPLETED/FAILED/CANCELLED/PAUSED.

    day_runner injectable for tests (defaults to run_day_pipeline).
    """
    runner = day_runner or run_day_pipeline
    conn = connect(db_path)
    job = get_job(conn, job_id)
    if not job:
        raise KeyError(f"job {job_id} not found")

    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"job_id": job_id, "status": job["status"], "message": "already finished"}

    if job["status"] == "PAUSED":
        return {"job_id": job_id, "status": "PAUSED", "message": "paused"}

    set_job_status(conn, job_id, "RUNNING", error=None)
    symbols = json.loads(job["symbols_json"])
    start = date.fromisoformat(job["start_day"])
    end = date.fromisoformat(job["end_day"])
    provider = job["provider"]
    days = daterange(start, end)
    total = len(symbols) * len(days)

    progress = {
        "done_days": 0,
        "total_days": total,
        "skipped": 0,
        "failed": 0,
        "success": 0,
        "incomplete": 0,
        "expected_empty": 0,
    }

    for sym in symbols:
        for day in days:
            # Refresh status for pause/cancel mid-run
            job = get_job(conn, job_id)
            assert job is not None
            if job["status"] == "PAUSED":
                update_job(conn, job_id, progress_json=json.dumps(progress))
                return {"job_id": job_id, "status": "PAUSED", "progress": progress}
            if job["status"] == "CANCELLED":
                update_job(conn, job_id, progress_json=json.dumps(progress))
                return {"job_id": job_id, "status": "CANCELLED", "progress": progress}

            update_job(
                conn,
                job_id,
                current_symbol=sym,
                current_day=day.isoformat(),
                current_hour=None,
                progress_json=json.dumps(progress),
            )

            if day_is_fully_verified(conn, sym, day.isoformat(), provider):
                upsert_job_day(conn, job_id, sym, day.isoformat(), "SKIPPED")
                progress["skipped"] += 1
                progress["done_days"] += 1
                continue

            try:
                out = runner(
                    sym,
                    day,
                    data_root=data_root,
                    db_path=db_path,
                    provider=provider,
                )
                st = out.get("status", "FAILED")
                if out.get("skipped"):
                    upsert_job_day(conn, job_id, sym, day.isoformat(), "SKIPPED")
                    progress["skipped"] += 1
                else:
                    upsert_job_day(conn, job_id, sym, day.isoformat(), st, out.get("error"))
                    if st == "SUCCESS":
                        progress["success"] += 1
                    elif st == "EXPECTED_EMPTY":
                        progress["expected_empty"] += 1
                    elif st == "INCOMPLETE":
                        progress["incomplete"] += 1
                    else:
                        progress["failed"] += 1
            except Exception as e:  # noqa: BLE001
                upsert_job_day(conn, job_id, sym, day.isoformat(), "FAILED", str(e))
                progress["failed"] += 1
                log.exception("day failed %s %s", sym, day)

            progress["done_days"] += 1
            update_job(conn, job_id, progress_json=json.dumps(progress))

    # Final status
    if progress["failed"] and progress["failed"] == progress["done_days"] - progress["skipped"]:
        final = "FAILED"
    elif progress["failed"] or progress["incomplete"]:
        # Partial: still COMPLETED with failures recorded (retryable via resume)
        final = "COMPLETED"
    else:
        final = "COMPLETED"

    err = None
    if progress["failed"]:
        err = f"{progress['failed']} day(s) failed"
    set_job_status(conn, job_id, final, error=err)
    update_job(conn, job_id, progress_json=json.dumps(progress), current_symbol=None, current_day=None)

    return {
        "job_id": job_id,
        "status": final,
        "progress": progress,
        "day_results": job_day_results(conn, job_id),
    }


def recover_stale_running_jobs(db_path: Path) -> list[int]:
    """On process start: RUNNING jobs become QUEUED so they can resume."""
    conn = connect(db_path)
    recovered = []
    for job in list_active_jobs(conn):
        if job["status"] == "RUNNING":
            set_job_status(conn, int(job["id"]), "QUEUED", error="recovered after restart")
            recovered.append(int(job["id"]))
    return recovered
