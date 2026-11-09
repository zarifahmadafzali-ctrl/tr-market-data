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


def archive_channel_id() -> int | None:
    """Prefer TELEGRAM_ARCHIVE_CHANNEL_ID; fall back to TELEGRAM_STORAGE_CHANNEL_ID."""
    for name in ("TELEGRAM_ARCHIVE_CHANNEL_ID", "TELEGRAM_STORAGE_CHANNEL_ID"):
        raw = (os.getenv(name) or "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return None


def archive_enabled() -> bool:
    # Explicit flag, or auto-enable when a channel id is configured
    if _bool("TELEGRAM_ARCHIVE_ENABLED", False):
        return True
    return archive_channel_id() is not None


def archive_max_retries() -> int:
    try:
        return max(0, int(os.getenv("ARCHIVE_MAX_RETRIES") or "8"))
    except ValueError:
        return 8


def archive_backoff_base_sec() -> int:
    try:
        return max(1, int(os.getenv("ARCHIVE_BACKOFF_BASE_SEC") or "30"))
    except ValueError:
        return 30


def archive_backoff_cap_sec() -> int:
    try:
        return max(1, int(os.getenv("ARCHIVE_BACKOFF_CAP_SEC") or "3600"))
    except ValueError:
        return 3600


def allow_delete_staging() -> bool:
    return _bool("ALLOW_DELETE_STAGING", False)

