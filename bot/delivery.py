"""Safe file delivery — prefer Telegram file_id (channel archive), fall back to local path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.config import TELEGRAM_MAX_UPLOAD_BYTES, data_root
from db.catalog import connect, get_day
from downloader.symbols import normalize_symbol


@dataclass
class FileOffer:
    path: Path | None
    file_id: str | None
    label: str
    size: int
    within_telegram_limit: bool
    source: str  # "channel" | "local"


def _safe_resolve(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        root_r = root.resolve()
        resolved.relative_to(root_r)
        return resolved
    except Exception:
        try:
            if path.is_file():
                return path.resolve()
        except Exception:
            return None
        return None


def catalog_files_for_day(
    db_path: Path,
    symbol: str,
    day: str,
    kind: str,
    *,
    provider: str = "dukascopy",
) -> list[FileOffer]:
    sym = normalize_symbol(symbol)
    conn = connect(db_path)
    row = get_day(conn, sym, day, provider)
    if not row:
        return []

    root = data_root()
    offers: list[FileOffer] = []

    def add(
        path_str: str | None,
        file_id: str | None,
        label: str,
    ) -> None:
        # Prefer channel file_id — works even when local disk is gone
        if file_id:
            offers.append(
                FileOffer(
                    path=None,
                    file_id=file_id,
                    label=label,
                    size=0,
                    within_telegram_limit=True,
                    source="channel",
                )
            )
            return

        if not path_str:
            return
        raw = Path(path_str)
        candidates: list[Path] = []
        if raw.is_absolute():
            candidates.append(raw)
        else:
            # Preferred: path relative to data root (provider=.../ticks.parquet)
            candidates.append(root / raw)
            # Legacy pilot rows stored "data/provider=..." while root is already .../data
            parts = raw.parts
            if parts and parts[0] == "data":
                candidates.append(root / Path(*parts[1:]))
            # Relative to project root (parent of data root)
            candidates.append(root.parent / raw)
        safe: Path | None = None
        for cand in candidates:
            # Allow files under data root OR project root/data
            for base in (root, root.parent):
                resolved = _safe_resolve(cand, base)
                if resolved is not None and resolved.is_file():
                    safe = resolved
                    break
            if safe is not None:
                break
            try:
                if cand.is_file():
                    safe = cand.resolve()
                    break
            except Exception:
                pass
        if safe is None or not safe.is_file():
            return
        size = safe.stat().st_size
        offers.append(
            FileOffer(
                path=safe,
                file_id=None,
                label=label,
                size=size,
                within_telegram_limit=size <= TELEGRAM_MAX_UPLOAD_BYTES and size > 0,
                source="local",
            )
        )

    if kind in ("tick", "both"):
        add(row.get("tick_file_path"), row.get("tick_file_id"), f"{sym} {day} ticks.parquet")
    if kind in ("1s", "both"):
        add(row.get("bars_1s_file_path"), row.get("bars_1s_file_id"), f"{sym} {day} bars_1s.parquet")
    add(row.get("metadata_path"), row.get("metadata_file_id"), f"{sym} {day} metadata.json")
    return offers


def day_ready(db_path: Path, symbol: str, day: str, kind: str, provider: str = "dukascopy") -> bool:
    sym = normalize_symbol(symbol)
    conn = connect(db_path)
    row = get_day(conn, sym, day, provider)
    if not row:
        return False
    if row["status"] not in ("SUCCESS", "EXPECTED_EMPTY"):
        return False

    # Ready if we have either a file_id or a local file status
    def has_tick() -> bool:
        if row.get("tick_file_id"):
            return True
        return row.get("tick_status") in ("SUCCESS", "EXPECTED_EMPTY")

    def has_bars() -> bool:
        if row.get("bars_1s_file_id"):
            return True
        return row.get("bars_1s_status") in ("SUCCESS", "EXPECTED_EMPTY")

    if kind == "tick":
        return has_tick()
    if kind == "1s":
        return has_bars()
    return has_tick() and has_bars()
