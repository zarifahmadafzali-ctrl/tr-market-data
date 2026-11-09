"""Environment configuration — never hardcode tokens."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def telegram_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def admin_ids() -> set[int]:
    raw = (os.getenv("TELEGRAM_ADMIN_IDS") or "").strip()
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


def storage_channel_id() -> int | None:
    """Private channel/group ID where finished files are archived.
    Bot must be admin with Post Messages. Example: -1001234567890
    """
    raw = (os.getenv("TELEGRAM_STORAGE_CHANNEL_ID") or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def sqlite_path() -> Path:
    p = os.getenv("SQLITE_PATH") or "db/tr_market_data.sqlite"
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / path
    return path


def data_root() -> Path:
    p = os.getenv("DATA_ROOT") or "data"
    path = Path(p)
    if not path.is_absolute():
        path = ROOT / path
    return path


def rate_limit_per_minute() -> int:
    try:
        return max(1, int(os.getenv("BOT_RATE_LIMIT_PER_MINUTE") or "20"))
    except ValueError:
        return 20


def public_access_enabled() -> bool:
    return _bool("BOT_PUBLIC_ACCESS", False)


# Telegram Bot API document limit ~50MB; stay under with margin
TELEGRAM_MAX_UPLOAD_BYTES = 45 * 1024 * 1024
