"""Bot services over Phase 2A job engine — no second downloader."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

from bot.manifest import lookup_package, rebuild_sqlite_from_manifest, fetch_manifest, empty_manifest
from bot.archive import (
    enqueue_archive_for_day,
    get_indexed_artifacts,
    package_fully_indexed,
)
from bot.config import archive_enabled
from bot.delivery import catalog_files_for_day, day_ready
from bot.parsers import GetRequest
from db.catalog import connect, get_job, job_day_results, list_active_jobs
from downloader.symbols import normalize_symbol
from scheduler.jobs import cancel_job, enqueue_range_job, run_job


def status_snapshot(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    active = list_active_jobs(conn)
    cur = conn.execute("SELECT COUNT(*) AS c FROM download_days")
    days = int(cur.fetchone()["c"])
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM download_days WHERE status = 'SUCCESS'"
    )
    ok = int(cur.fetchone()["c"])
    archive_stats = {"enabled": archive_enabled(), "indexed_artifacts": 0, "pending": 0}
    try:
        archive_stats["indexed_artifacts"] = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM archived_artifacts WHERE status = 'INDEXED'"
            ).fetchone()["c"]
        )
        archive_stats["pending"] = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM archived_artifacts WHERE status IN ('PENDING','FAILED','UPLOADING')"
            ).fetchone()["c"]
        )
    except Exception:
        pass
    return {
        "catalog_days": days,
        "success_days": ok,
        "active_jobs": len(active),
        "archive": archive_stats,
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



def _hydrate_from_manifest(db_path: Path, symbol: str, day: str, provider: str = "dukascopy") -> dict:
    """Pull one package from the GitHub manifest into SQLite when missing locally."""
    from bot.config import manifest_enabled, manifest_token, manifest_repo
    from bot.archive import package_key, recompute_package_status, STATUS_INDEXED
    from bot.manifest import fetch_manifest, rebuild_sqlite_from_manifest
    from downloader.symbols import normalize_symbol

    if not manifest_enabled() or not manifest_token() or not manifest_repo():
        return {"status": "disabled"}
    sym = normalize_symbol(symbol)
    pk = package_key(provider, sym, day)
    manifest, _sha, st = fetch_manifest()
    if st != "ok" or not manifest:
        return {"status": st or "error"}
    packages = manifest.get("packages") or {}
    if pk not in packages:
        return {"status": "missing_package"}
    # Rebuild only this package by constructing a filtered manifest
    filtered = {
        "schema_version": manifest.get("schema_version", 1),
        "updated_at": manifest.get("updated_at", ""),
        "packages": {pk: packages[pk]},
    }
    stats = rebuild_sqlite_from_manifest(db_path, filtered)
    return {"status": "ok", "stats": stats, "package_key": pk}



def handle_get_request(
    req: GetRequest,
    *,
    db_path: Path,
    data_root: Path,
    run_immediately: bool = False,
    requester_chat_id: int | None = None,
    requester_user_id: int | None = None,
) -> dict[str, Any]:
    """Check archive → local disk → enqueue download. No second downloader."""
    days: list[date] = []
    d = req.start
    while d <= req.end:
        days.append(d)
        d += timedelta(days=1)

    ready_days: list[str] = []
    missing_days: list[str] = []
    archive_files: list[dict[str, Any]] = []
    local_files: list[dict[str, Any]] = []
    archive_partial_days: list[str] = []

    for day in days:
        iso = day.isoformat()
        # Optional durable index: hydrate SQLite from GitHub manifest when local miss
        if archive_enabled() and not package_fully_indexed(db_path, req.symbol, iso, req.kind):
            try:
                _hydrate_from_manifest(db_path, req.symbol, iso)
            except Exception:
                pass
        if archive_enabled() and package_fully_indexed(db_path, req.symbol, iso, req.kind):
            ready_days.append(iso)
            for row in get_indexed_artifacts(db_path, req.symbol, iso, req.kind):
                archive_files.append(
                    {
                        "source": "telegram_archive",
                        "file_id": row["telegram_file_id"],
                        "file_unique_id": row.get("telegram_file_unique_id"),
                        "label": f"{row['symbol']} {row['day']} {row['file_name']} (archive)",
                        "file_name": row["file_name"],
                        "size": row.get("size_bytes") or 0,
                        "artifact_type": row["artifact_type"],
                        "within_telegram_limit": True,
                    }
                )
            continue

        if day_ready(db_path, req.symbol, iso, req.kind):
            ready_days.append(iso)
            if archive_enabled():
                try:
                    enqueue_archive_for_day(
                        db_path,
                        req.symbol,
                        iso,
                        root=data_root,
                        requester_chat_id=requester_chat_id,
                        requester_user_id=requester_user_id,
                    )
                    if not package_fully_indexed(db_path, req.symbol, iso, req.kind):
                        archive_partial_days.append(iso)
                except Exception:
                    archive_partial_days.append(iso)
            for offer in catalog_files_for_day(db_path, req.symbol, iso, req.kind):
                local_files.append(
                    {
                        "source": "local_disk",
                        "path": str(offer.path),
                        "label": offer.label,
                        "size": offer.size,
                        "within_telegram_limit": offer.within_telegram_limit,
                    }
                )
        else:
            missing_days.append(iso)

    job_id = None
    job_result = None
    if missing_days:
        job_id = enqueue_range_job(
            db_path,
            symbols=[req.symbol],
            start_day=req.start,
            end_day=req.end,
        )
        if run_immediately:
            job_result = run_job(db_path, job_id, data_root=data_root)
            if archive_enabled():
                for iso in list(missing_days):
                    if day_ready(db_path, req.symbol, iso, req.kind):
                        enqueue_archive_for_day(
                            db_path,
                            req.symbol,
                            iso,
                            root=data_root,
                            requester_chat_id=requester_chat_id,
                            requester_user_id=requester_user_id,
                        )

    files = archive_files + local_files
    return {
        "symbol": normalize_symbol(req.symbol),
        "kind": req.kind,
        "ready_days": ready_days,
        "missing_days": missing_days,
        "archive_partial_days": archive_partial_days,
        "files": files,
        "archive_files": archive_files,
        "local_files": local_files,
        "job_id": job_id,
        "job_result": job_result,
    }




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


def list_jobs(db_path: Path) -> list[dict[str, Any]]:
    conn = connect(db_path)
    return [dict(j) for j in list_active_jobs(conn)] + _recent_jobs(conn, limit=15)


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
