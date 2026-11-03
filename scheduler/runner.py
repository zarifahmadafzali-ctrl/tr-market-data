"""Simple cooperative scheduler — one job at a time, no overlap for same range."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from db.catalog import connect, get_job, list_active_jobs
from scheduler.jobs import recover_stale_running_jobs, run_job

log = logging.getLogger(__name__)


class DownloadScheduler:
    def __init__(self, db_path: Path, data_root: Path):
        self.db_path = db_path
        self.data_root = data_root
        self._paused = False

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def tick(self) -> dict[str, Any] | None:
        """Run next QUEUED job if scheduler is not paused. Returns job result or None."""
        if self._paused:
            return None
        recover_stale_running_jobs(self.db_path)
        conn = connect(self.db_path)
        queued = [j for j in list_active_jobs(conn) if j["status"] == "QUEUED"]
        if not queued:
            return None
        # One at a time — lowest id first
        job_id = int(queued[0]["id"])
        log.info("scheduler starting job %s", job_id)
        return run_job(self.db_path, job_id, data_root=self.data_root)

    def status(self) -> dict[str, Any]:
        conn = connect(self.db_path)
        return {
            "scheduler_paused": self._paused,
            "active_jobs": list_active_jobs(conn),
        }
