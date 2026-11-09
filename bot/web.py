"""Single-process Render Web Service entrypoint.

Binds HTTP on $PORT (required by Render Web Services), runs Telegram polling
and the download worker in background threads sharing the same SQLite + DATA_ROOT.

Local / production:
  python -m bot.web

Do not run multiple instances of this process — Telegram allows only one getUpdates consumer.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from bot.archive import archive_tick, recover_stale_uploading, reconcile_success_days
from bot.config import archive_enabled, data_root, sqlite_path, telegram_token, archive_channel_id
from scheduler.jobs import recover_stale_running_jobs
from scheduler.runner import DownloadScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Avoid logging full Telegram URLs that embed the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("bot.web")

_stop = threading.Event()


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/health", "/healthz"):
            body = b'{"status":"ok","service":"tr-market-data"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"not found\n"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quiet access log
        return


def _serve_health() -> None:
    port = int(os.environ.get("PORT") or "10000")
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info("Health HTTP listening on 0.0.0.0:%s", port)
    server.timeout = 1.0
    while not _stop.is_set():
        server.handle_request()
    try:
        server.server_close()
    except Exception:
        pass


def _run_worker() -> None:
    db = sqlite_path()
    data = data_root()
    try:
        recover_stale_running_jobs(db)
    except Exception as e:
        log.exception("recover_stale_running_jobs failed: %s", e)
    if archive_enabled():
        try:
            n = recover_stale_uploading(db)
            log.info("Archive recovery: %s row(s); channel_id=%s", n, archive_channel_id())
            reconcile_success_days(db)
        except Exception as e:
            log.exception("archive recovery failed: %s", e)
    else:
        log.info("Telegram archive disabled (TELEGRAM_ARCHIVE_ENABLED not true)")
    sched = DownloadScheduler(db, data)
    log.info("Worker loop started db=%s data=%s archive=%s", db, data, archive_enabled())
    while not _stop.is_set():
        try:
            result = sched.tick()
            if result:
                log.info("Job finished: %s", result.get("status"))
            arch = None
            if archive_enabled():
                try:
                    arch = archive_tick(db)
                    if arch:
                        log.info("Archive step: %s", arch.get("status"))
                except Exception as e:
                    log.exception("archive tick error: %s", e)
            if not result and not arch:
                _stop.wait(5)
        except Exception as e:
            log.exception("Worker tick error: %s", e)
            _stop.wait(10)


def _run_bot() -> None:
    # Import here so health can bind even if token is missing (clear error path).
    from bot.app import build_app

    app = build_app()
    log.info("Telegram polling starting (admin-only)")
    # run_polling blocks until stop; stop_signals empty — we handle SIGTERM ourselves
    app.run_polling(
        allowed_updates=["message", "callback_query"],
        stop_signals=None,
        drop_pending_updates=False,
    )


def main() -> None:
    if not telegram_token():
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Set it in Render Environment (never commit)."
        )

    def _on_signal(signum, _frame) -> None:
        log.info("Signal %s received — shutting down", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    health_t = threading.Thread(target=_serve_health, name="health", daemon=True)
    worker_t = threading.Thread(target=_run_worker, name="worker", daemon=True)
    health_t.start()
    worker_t.start()

    try:
        _run_bot()
    except Exception as e:
        log.exception("Bot exited: %s", e)
        _stop.set()
        raise
    finally:
        _stop.set()
        log.info("bot.web exiting")


if __name__ == "__main__":
    main()
