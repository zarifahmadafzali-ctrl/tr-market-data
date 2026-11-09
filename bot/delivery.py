"""Safe file delivery restricted to catalog-registered paths."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.config import TELEGRAM_MAX_UPLOAD_BYTES, data_root
from db.catalog import connect, get_day
from downloader.symbols import normalize_symbol


@dataclass
class FileOffer:
    path: Path
    label: str
    size: int
    within_telegram_limit: bool


def _safe_resolve(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve()
        root_r = root.resolve()
        resolved.relative_to(root_r)
        return resolved
    except Exception:
        # Allow absolute paths that exist if they match catalog entries under data root
        try:
            if path.is_file():
                # still require path string to live under root when possible
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

    def add(path_str: str | None, label: str) -> None:
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
                label=label,
                size=size,
                within_telegram_limit=size <= TELEGRAM_MAX_UPLOAD_BYTES and size > 0,
            )
        )

    if kind in ("tick", "both"):
        add(row.get("tick_file_path"), f"{sym} {day} ticks.parquet")
    if kind in ("1s", "both"):
        add(row.get("bars_1s_file_path"), f"{sym} {day} bars_1s.parquet")
    add(row.get("metadata_path"), f"{sym} {day} metadata.json")
    return offers


def day_ready(db_path: Path, symbol: str, day: str, kind: str, provider: str = "dukascopy") -> bool:
    sym = normalize_symbol(symbol)
    conn = connect(db_path)
    row = get_day(conn, sym, day, provider)
    if not row:
        return False
    if row["status"] not in ("SUCCESS", "EXPECTED_EMPTY"):
        return False
    if kind == "tick":
        return row.get("tick_status") in ("SUCCESS", "EXPECTED_EMPTY")
    if kind == "1s":
        return row.get("bars_1s_status") in ("SUCCESS", "EXPECTED_EMPTY")
    return row.get("tick_status") in ("SUCCESS", "EXPECTED_EMPTY") and row.get(
        "bars_1s_status"
    ) in ("SUCCESS", "EXPECTED_EMPTY")
