"""Archive finished Parquet files to a private Telegram channel.

The channel acts as durable cloud storage. After upload we keep the file_id
in SQLite so the bot can re-send the same file later even if local disk is gone.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.error import TelegramError

from bot.config import storage_channel_id, telegram_token, TELEGRAM_MAX_UPLOAD_BYTES
from db.catalog import connect, get_day, set_day_file_ids

log = logging.getLogger(__name__)


def _bot() -> Bot | None:
    token = telegram_token()
    if not token:
        return None
    return Bot(token=token)


def caption_for(symbol: str, day: str, kind: str) -> str:
    return f"{symbol} | {day} | {kind}"


async def upload_day_to_channel(
    *,
    db_path: Path,
    symbol: str,
    day: str,
    tick_path: Path | None = None,
    bars_path: Path | None = None,
    metadata_path: Path | None = None,
    provider: str = "dukascopy",
) -> dict[str, Any]:
    """Upload available local files for a day to the storage channel.

    Returns a dict with any new file_ids that were stored.
    Safe to call multiple times — skips files that already have a file_id.
    """
    channel = storage_channel_id()
    if channel is None:
        log.debug("TELEGRAM_STORAGE_CHANNEL_ID not set — skip channel upload")
        return {"skipped": True, "reason": "no_channel"}

    bot = _bot()
    if bot is None:
        return {"skipped": True, "reason": "no_token"}

    conn = connect(db_path)
    row = get_day(conn, symbol, day, provider) or {}

    result: dict[str, Any] = {
        "symbol": symbol,
        "day": day,
        "uploaded": [],
        "errors": [],
    }
    tick_file_id = row.get("tick_file_id")
    bars_file_id = row.get("bars_1s_file_id")
    meta_file_id = row.get("metadata_file_id")
    message_ids: list[int] = []
    try:
        if row.get("storage_message_ids"):
            message_ids = json.loads(row["storage_message_ids"])
    except Exception:
        message_ids = []

    async def _send(path: Path | None, kind: str, existing_id: str | None) -> str | None:
        if existing_id:
            return existing_id
        if path is None or not path.is_file():
            return None
        size = path.stat().st_size
        if size <= 0:
            return None
        if size > TELEGRAM_MAX_UPLOAD_BYTES:
            result["errors"].append(f"{kind}: {size} bytes exceeds Telegram limit")
            log.warning("Skip %s %s %s — too large (%s bytes)", symbol, day, kind, size)
            return None
        try:
            with path.open("rb") as fh:
                msg = await bot.send_document(
                    chat_id=channel,
                    document=fh,
                    filename=path.name,
                    caption=caption_for(symbol, day, kind),
                    disable_notification=True,
                )
            doc = msg.document
            if not doc:
                result["errors"].append(f"{kind}: no document in response")
                return None
            message_ids.append(msg.message_id)
            result["uploaded"].append(kind)
            log.info("Archived %s %s %s → file_id=%s msg=%s", symbol, day, kind, doc.file_id, msg.message_id)
            return doc.file_id
        except TelegramError as e:
            result["errors"].append(f"{kind}: {e}")
            log.exception("Channel upload failed %s %s %s", symbol, day, kind)
            return None

    tick_file_id = await _send(tick_path, "tick", tick_file_id)
    bars_file_id = await _send(bars_path, "1s", bars_file_id)
    meta_file_id = await _send(metadata_path, "meta", meta_file_id)

    if tick_file_id or bars_file_id or meta_file_id or message_ids:
        set_day_file_ids(
            conn,
            symbol,
            day,
            provider=provider,
            tick_file_id=tick_file_id,
            bars_1s_file_id=bars_file_id,
            metadata_file_id=meta_file_id,
            storage_message_ids=json.dumps(message_ids) if message_ids else None,
        )

    result["tick_file_id"] = tick_file_id
    result["bars_1s_file_id"] = bars_file_id
    result["metadata_file_id"] = meta_file_id
    result["message_ids"] = message_ids
    return result


def upload_day_to_channel_sync(
    *,
    db_path: Path,
    symbol: str,
    day: str,
    tick_path: Path | None = None,
    bars_path: Path | None = None,
    metadata_path: Path | None = None,
    provider: str = "dukascopy",
) -> dict[str, Any]:
    """Sync wrapper for use from the worker / pipeline (no running event loop)."""
    import asyncio

    return asyncio.run(
        upload_day_to_channel(
            db_path=db_path,
            symbol=symbol,
            day=day,
            tick_path=tick_path,
            bars_path=bars_path,
            metadata_path=metadata_path,
            provider=provider,
        )
    )
