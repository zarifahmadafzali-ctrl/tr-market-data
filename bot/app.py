"""Telegram bot entry (stub). Enable after TELEGRAM_BOT_TOKEN is set."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN missing — copy .env.example to .env")
    log.info("Bot token present. Wire python-telegram-bot handlers next.")
    # Handlers intentionally not started in scaffold-only commit.


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
