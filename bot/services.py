"""Bot services over Phase 2A job engine — no second downloader."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bot.delivery import catalog_files_for_day, day_ready
from bot.parsers import GetRequest
from db.catalog import connect, get_day, get_job, job_day_results, list_active_jobs
from downloader.symbols import normalize_symbol
from scheduler.jobs import cancel_job, enqueue_range_job, run_job
from storage.paths import day_dir


def status_snapshot(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    active = list_active_jobs(conn)
    cur = conn.execute("SELECT COUNT(*) AS c FROM download_days")
    days = int(cur.fetchone()["c"])
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM download_days WHERE status = 'SUCCESS'"
    )
    ok = int(cur.fetchone()["c"])
    return {
        "catalog_days": days,
        "success_days": ok,
        "active_jobs": len(active),
        "jobs": [
            {
                "id": j["id"],
                "status": j["status"],
                "symbols": j["symbols_json"],
                "range": f"{j['start_day']} → {j['end_day']}",
                "progress": j.get("progress_json"),
            }
            for j in active[:20]
        ],
    }


def handle_get_request(
    req: GetRequest,
    *,
    db_path: Path,
    data_root: Path,
    run_immediately: bool = False,
) -> dict[str, Any]:
    """Check catalog; deliver if ready; else enqueue Phase 2A job."""
    days = []
    d = req.start
    while d <= req.end:
        days.append(d)
        d += timedelta(days=1)

    ready_days: list[str] = []
    missing_days: list[str] = []
    for day in days:
        iso = day.isoformat()
        if day_ready(db_path, req.symbol, iso, req.kind):
            ready_days.append(iso)
        else:
            missing_days.append(iso)

    files: list[dict[str, Any]] = []
    for iso in ready_days:
        for offer in catalog_files_for_day(db_path, req.symbol, iso, req.kind):
            files.append(
                {
                    "path": str(offer.path),
                    "label": offer.label,
                    "size": offer.size,
                    "within_telegram_limit": offer.within_telegram_limit,
                }
            )

    job_id = None
    job_result = None
    if missing_days:
        job_id = enqueue_range_job(
            db_path,
            symbols=[req.symbol],
            start_day=req.start,
            end_day=req.end,
            provider="dukascopy",
        )
        if run_immediately:
            job_result = run_job(db_path, job_id, data_root=data_root)

    return {
        "symbol": normalize_symbol(req.symbol),
        "kind": req.kind,
        "ready_days": ready_days,
        "missing_days": missing_days,
        "files": files,
        "job_id": job_id,
        "job_result": job_result,
    }


def list_jobs(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    return list_active_jobs(conn) + _recent_jobs(conn, limit=15)


def _recent_jobs(conn, limit: int = 15) -> list[dict[str, Any]]:
    cur = conn.execute(
        """
        SELECT * FROM download_jobs
        WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
        ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    return [dict(r) for r in cur.fetchall()]


def cancel_user_job(db_path: Path, job_id: int) -> dict[str, Any]:
    conn = connect(db_path)
    job = get_job(conn, job_id)
    if not job:
        return {"ok": False, "error": "Job not found"}
    if job["status"] in ("COMPLETED", "CANCELLED"):
        return {"ok": False, "error": f"Job already {job['status']}"}
    cancel_job(db_path, job_id)
    return {"ok": True, "job_id": job_id, "status": "CANCELLED"}


def job_progress(db_path: Path, job_id: int) -> dict[str, Any] | None:
    conn = connect(db_path)
    job = get_job(conn, job_id)
    if not job:
        return None
    return {
        "job": dict(job),
        "days": job_day_results(conn, job_id),
    }
