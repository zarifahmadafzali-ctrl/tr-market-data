"""Background worker: process QUEUED download jobs without blocking the bot.

Run as a separate process sharing the same SQLITE_PATH and DATA_ROOT.
"""

from __future__ import annotations

import logging
import time

from bot.config import data_root, sqlite_path
from scheduler.jobs import recover_stale_running_jobs
from scheduler.runner import DownloadScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot.worker")


def main() -> None:
    db = sqlite_path()
    data = data_root()
    recover_stale_running_jobs(db)
    sched = DownloadScheduler(db, data)
    log.info("Worker started db=%s data=%s", db, data)
    while True:
        result = sched.tick()
        if result:
            log.info("Job finished: %s", result.get("status"))
        else:
            time.sleep(5)


if __name__ == "__main__":
    main()
