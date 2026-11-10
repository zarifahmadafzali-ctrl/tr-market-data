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



def manifest_enabled() -> bool:
    return _bool("ARCHIVE_MANIFEST_ENABLED", False)


def manifest_repo() -> str:
    return (os.getenv("ARCHIVE_MANIFEST_REPO") or "").strip()


def manifest_branch() -> str:
    return (os.getenv("ARCHIVE_MANIFEST_BRANCH") or "main").strip() or "main"


def manifest_path() -> str:
    return (os.getenv("ARCHIVE_MANIFEST_PATH") or "archive_manifest.json").strip() or "archive_manifest.json"


def manifest_token() -> str:
    """GitHub PAT for private manifest repo. Never log this value."""
    return (os.getenv("ARCHIVE_MANIFEST_TOKEN") or "").strip()


def sqlite_backup_enabled() -> bool:
    """Mirror SQLite catalog file to private GitHub (binary Contents API)."""
    return _bool("SQLITE_BACKUP_ENABLED", False)


def sqlite_backup_repo() -> str:
    # Prefer dedicated override; fall back to manifest repo
    return (os.getenv("SQLITE_BACKUP_REPO") or os.getenv("ARCHIVE_MANIFEST_REPO") or "").strip()


def sqlite_backup_branch() -> str:
    return (
        (os.getenv("SQLITE_BACKUP_BRANCH") or os.getenv("ARCHIVE_MANIFEST_BRANCH") or "main").strip()
        or "main"
    )


def sqlite_backup_path() -> str:
    """Path inside the GitHub repo for the sqlite file."""
    return (
        (os.getenv("SQLITE_BACKUP_PATH") or "backups/tr_market_data.sqlite").strip()
        or "backups/tr_market_data.sqlite"
    )


def sqlite_backup_token() -> str:
    return (
        (os.getenv("SQLITE_BACKUP_TOKEN") or os.getenv("ARCHIVE_MANIFEST_TOKEN") or "").strip()
    )


def sqlite_backup_min_interval_sec() -> int:
    """Minimum seconds between uploads (rate-limit Git history growth)."""
    try:
        return max(30, int(os.getenv("SQLITE_BACKUP_MIN_INTERVAL_SEC") or "120"))
    except ValueError:
        return 120


def sqlite_backup_max_bytes() -> int:
    """Refuse upload if snapshot exceeds this (GitHub Contents practical limit)."""
    try:
        return max(1_000_000, int(os.getenv("SQLITE_BACKUP_MAX_BYTES") or str(40 * 1024 * 1024)))
    except ValueError:
        return 40 * 1024 * 1024
