"""Engine configuration from environment (GitHub Actions secrets / vars)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or str(default))
    except ValueError:
        return default


@dataclass(frozen=True)
class EngineConfig:
    # GitHub private index repo (manifest + queue/)
    index_repo: str
    index_token: str
    index_branch: str
    queue_prefix: str
    manifest_path: str

    # Telegram
    telegram_token: str
    archive_channel_id: int | None
    admin_ids: frozenset[int]

    # Local paths inside the runner
    sqlite_path: Path
    data_root: Path

    # Time budget (Actions jobs ~6h; leave margin)
    max_seconds: int
    day_margin_sec: int
    stale_sec: int
    max_archive_steps_per_day: int

    # Optional workflow re-dispatch
    workflow_repo: str  # OWNER/NAME of the code repo that hosts the workflow
    workflow_id: str  # filename or id, e.g. engine.yml
    github_token_for_dispatch: str  # often same as index_token if scopes allow

    provider: str = "dukascopy"

    @property
    def ready(self) -> bool:
        return bool(self.index_repo and self.index_token and self.telegram_token)


def load_config() -> EngineConfig:
    admin_raw = (os.getenv("TELEGRAM_ADMIN_IDS") or "").strip()
    admins: set[int] = set()
    for part in admin_raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit() or (part.startswith("-") and part[1:].isdigit()):
            admins.add(int(part))

    ch_raw = (
        os.getenv("TELEGRAM_ARCHIVE_CHANNEL_ID")
        or os.getenv("TELEGRAM_STORAGE_CHANNEL_ID")
        or ""
    ).strip()
    channel: int | None
    try:
        channel = int(ch_raw) if ch_raw else None
    except ValueError:
        channel = None

    index_token = (
        os.getenv("ARCHIVE_MANIFEST_TOKEN")
        or os.getenv("ENGINE_INDEX_TOKEN")
        or os.getenv("GITHUB_TOKEN")
        or ""
    ).strip()
    index_repo = (
        os.getenv("ARCHIVE_MANIFEST_REPO")
        or os.getenv("ENGINE_INDEX_REPO")
        or ""
    ).strip()

    sqlite = Path(os.getenv("SQLITE_PATH") or str(ROOT / "db" / "tr_market_data.sqlite"))
    if not sqlite.is_absolute():
        sqlite = ROOT / sqlite
    data = Path(os.getenv("DATA_ROOT") or str(ROOT / "data"))
    if not data.is_absolute():
        data = ROOT / data

    return EngineConfig(
        index_repo=index_repo,
        index_token=index_token,
        index_branch=(os.getenv("ARCHIVE_MANIFEST_BRANCH") or os.getenv("ENGINE_INDEX_BRANCH") or "main").strip()
        or "main",
        queue_prefix=(os.getenv("ENGINE_QUEUE_PREFIX") or "queue").strip().strip("/") or "queue",
        manifest_path=(os.getenv("ARCHIVE_MANIFEST_PATH") or "archive_manifest.json").strip()
        or "archive_manifest.json",
        telegram_token=(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip(),
        archive_channel_id=channel,
        admin_ids=frozenset(admins),
        sqlite_path=sqlite,
        data_root=data,
        max_seconds=_int("ENGINE_MAX_SECONDS", 19200),
        day_margin_sec=_int("ENGINE_DAY_MARGIN_SEC", 900),
        stale_sec=_int("ENGINE_STALE_SEC", 900),
        max_archive_steps_per_day=_int("ENGINE_MAX_ARCHIVE_STEPS", 24),
        workflow_repo=(
            os.getenv("ENGINE_WORKFLOW_REPO")
            or os.getenv("GITHUB_REPOSITORY")
            or "zarifahmadafzali-ctrl/tr-market-data"
        ).strip(),
        workflow_id=(os.getenv("ENGINE_WORKFLOW_ID") or "engine.yml").strip() or "engine.yml",
        github_token_for_dispatch=(
            os.getenv("ENGINE_DISPATCH_TOKEN")
            or index_token
            or (os.getenv("GITHUB_TOKEN") or "")
        ).strip(),
        provider=(os.getenv("ENGINE_PROVIDER") or "dukascopy").strip() or "dukascopy",
    )
