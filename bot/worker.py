"""Background worker: process QUEUED download jobs + Telegram archive uploads.

Run as a separate process sharing the same SQLITE_PATH and DATA_ROOT.
"""

from __future__ import annotations

import logging
import time

from bot.archive import archive_tick, recover_stale_uploading, reconcile_success_days
from bot.config import archive_enabled, data_root, sqlite_path
from scheduler.jobs import recover_stale_running_jobs
from scheduler.runner import DownloadScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot.worker")


def main() -> None:
    db = sqlite_path()
    data = data_root()
    recover_stale_running_jobs(db)
    if archive_enabled():
        n = recover_stale_uploading(db)
        log.info("Archive recovery: %s artifact row(s)", n)
        try:
            reconcile_success_days(db)
        except Exception as e:
            log.warning("archive reconcile: %s", e)
    sched = DownloadScheduler(db, data)
    log.info("Worker started db=%s data=%s archive=%s", db, data, archive_enabled())
    while True:
        result = sched.tick()
        if result:
            log.info("Download job finished: %s", result.get("status"))
        arch = None
        if archive_enabled():
            try:
                arch = archive_tick(db)
                if arch:
                    log.info("Archive step: %s", arch.get("status"))
            except Exception as e:
                log.exception("archive tick error: %s", e)
        if not result and not arch:
            time.sleep(5)


if __name__ == "__main__":
    main()
