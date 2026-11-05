"""Telegram bot entrypoint — connects UI to Phase 2A job engine."""

from __future__ import annotations

import logging
import sys

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from bot.config import admin_ids, telegram_token
from bot.handlers import (
    cmd_cancel,
    cmd_get,
    cmd_help,
    cmd_jobs,
    cmd_start,
    cmd_status,
    cmd_symbols,
    on_callback,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot.app")


def build_app() -> Application:
    token = telegram_token()
    if not token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN is not set. Export it or put it in .env (never commit)."
        )
    if not admin_ids():
        log.warning("TELEGRAM_ADMIN_IDS is empty — all users will be denied.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("symbols", cmd_symbols))
    app.add_handler(CommandHandler("get", cmd_get))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app


def main() -> None:
    app = build_app()
    log.info("Starting TRMarketDataBot (admin-only Phase 2B)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
