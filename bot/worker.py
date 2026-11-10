"""Background worker: process QUEUED download jobs + Telegram archive uploads.

Run as a separate process sharing the same SQLITE_PATH and DATA_ROOT.
"""

from __future__ import annotations

import logging
import time

from bot.archive import archive_tick, recover_stale_uploading, reconcile_success_days
from bot.manifest import startup_sync_manifest
from bot.sqlite_backup import push_sqlite_to_github, restore_sqlite_from_github
from bot.config import archive_enabled, data_root, sqlite_path
from scheduler.jobs import recover_stale_running_jobs
from scheduler.runner import DownloadScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot.worker")


def main() -> None:
    db = sqlite_path()
    data = data_root()
    try:
        br = restore_sqlite_from_github(db)
        log.info("SQLite GitHub restore status=%s", br.get("status"))
    except Exception as e:
        log.warning("sqlite restore: %s", e)
    recover_stale_running_jobs(db)
    if archive_enabled():
        n = recover_stale_uploading(db)
        log.info("Archive recovery: %s artifact row(s)", n)
        try:
            reconcile_success_days(db)
        except Exception as e:
            log.warning("archive reconcile: %s", e)
    try:
        sync = startup_sync_manifest(db)
        log.info("GitHub manifest sync status=%s", sync.get("status"))
    except Exception as e:
        log.warning("manifest startup sync: %s", e)
    sched = DownloadScheduler(db, data)
    log.info("Worker started db=%s data=%s archive=%s", db, data, archive_enabled())
    while True:
        result = sched.tick()
        if result:
            log.info("Download job finished: %s", result.get("status"))
            try:
                push_sqlite_to_github(db, reason=f"job_{result.get('status')}", force=True)
            except Exception as e:
                log.warning("sqlite backup after job: %s", e)
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
