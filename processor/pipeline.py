"""Day pipeline: download → ticks parquet → 1s bars parquet → catalog → channel archive."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

from db.catalog import (
    connect,
    get_hour_statuses,
    hours_to_fetch,
    upsert_day,
    upsert_hour,
    upsert_symbol,
)
from downloader.dukascopy import download_day
from downloader.symbols import normalize_symbol
from processor.ohlc import ticks_to_seconds
from storage.parquet_io import (
    read_bars_parquet,
    read_ticks_parquet,
    write_bars_parquet,
    write_metadata,
    write_ticks_parquet,
)
from storage.paths import bars_1s_parquet_path, day_dir, metadata_json_path, ticks_parquet_path

log = logging.getLogger(__name__)


def run_day_pipeline(
    symbol: str,
    day: date,
    *,
    data_root: Path,
    db_path: Path,
    provider: str = "dukascopy",
    force: bool = False,
) -> dict[str, Any]:
    """Download one day with hour-level resume and independent tick/bars status."""
    sym = normalize_symbol(symbol)
    db = connect(db_path)
    upsert_symbol(db, sym, sym, description=f"{symbol} via Dukascopy")

    result: dict[str, Any] = {
        "symbol": sym,
        "day": day.isoformat(),
        "provider": provider,
        "status": "RUNNING",
        "skipped": False,
    }

    # Skip fully verified days unless force
    from db.catalog import day_is_fully_verified, get_day

    if not force and day_is_fully_verified(db, sym, day.isoformat(), provider):
        existing = get_day(db, sym, day.isoformat(), provider) or {}
        result.update(
            {
                "status": "SUCCESS",
                "skipped": True,
                "tick_status": existing.get("tick_status"),
                "bars_1s_status": existing.get("bars_1s_status"),
                "tick_count": existing.get("tick_count", 0),
                "bars_1s_count": existing.get("bars_1s_count", 0),
                "successful_hours": existing.get("successful_hours", 0),
                "empty_hours": existing.get("empty_hours", 0),
                "failed_hours": existing.get("failed_hours", 0),
            }
        )
        # Still try to archive if file_ids are missing
        _try_archive(db_path, sym, day.isoformat(), provider, existing)
        return result

    need_hours = list(range(24)) if force else hours_to_fetch(db, sym, day.isoformat(), provider)
    known = get_hour_statuses(db, sym, day.isoformat(), provider)

    # Seed classification-only placeholders for already-good hours
    seed = [
        {"hour": h, "ticks": [], "classification": known[h], "status": 0, "error": None, "attempts": 0}
        for h in range(24)
        if h not in need_hours and known.get(h) in ("SUCCESS", "EXPECTED_EMPTY")
    ]

    try:
        def _persist_hour(hr: dict[str, Any]) -> None:
            upsert_hour(
                db,
                {
                    "symbol": sym,
                    "provider": provider,
                    "day": day.isoformat(),
                    "hour": hr["hour"],
                    "status": hr["classification"],
                    "tick_count": len(hr.get("ticks") or []),
                    "http_status": hr.get("status"),
                    "attempts": hr.get("attempts"),
                    "error": hr.get("error"),
                },
            )

        if need_hours:
            downloaded = asyncio.run(
                download_day(
                    symbol,
                    day,
                    hours=need_hours,
                    seed_hour_results=seed,
                    on_hour=_persist_hour,
                )
            )
        else:
            downloaded = {
                "symbol": sym,
                "day": day.isoformat(),
                "status": "SUCCESS",
                "hours": seed,
                "ticks": [],
                "tick_count": 0,
                "successful_hours": sum(1 for h in seed if h["classification"] == "SUCCESS"),
                "empty_hours": sum(1 for h in seed if h["classification"] == "EXPECTED_EMPTY"),
                "failed_hours": 0,
            }
    except Exception as e:  # noqa: BLE001
        upsert_day(
            db,
            {
                "symbol": sym,
                "provider": provider,
                "day": day.isoformat(),
                "status": "FAILED",
                "tick_status": "FAILED",
                "bars_1s_status": "FAILED",
                "error": str(e),
            },
        )
        result.update({"status": "FAILED", "error": str(e)})
        return result

    # Persist hour statuses for newly fetched hours
    for h in downloaded["hours"]:
        if int(h["hour"]) in need_hours or force:
            upsert_hour(
                db,
                {
                    "symbol": sym,
                    "provider": provider,
                    "day": day.isoformat(),
                    "hour": h["hour"],
                    "status": h["classification"],
                    "tick_count": len(h.get("ticks") or []),
                    "http_status": h.get("status"),
                    "attempts": h.get("attempts"),
                    "error": h.get("error"),
                },
            )

    dpath = day_dir(data_root, provider=provider, symbol=sym, day=day)
    tpath = ticks_parquet_path(dpath)
    bpath = bars_1s_parquet_path(dpath)
    mpath = metadata_json_path(dpath)

    # Merge new ticks with any existing parquet ticks
    new_ticks = list(downloaded.get("ticks") or [])
    existing_ticks: list[dict[str, Any]] = []
    if tpath.exists() and tpath.stat().st_size > 0:
        try:
            existing_ticks = read_ticks_parquet(tpath)
        except Exception:  # noqa: BLE001
            existing_ticks = []

    merged = _merge_ticks(existing_ticks, new_ticks)

    tick_status = "FAILED"
    bars_status = "FAILED"
    tick_count = 0
    bars_count = 0
    error: str | None = None
    day_status = downloaded["status"]

    # Recompute day status from hour catalog after upserts
    hour_map = get_hour_statuses(db, sym, day.isoformat(), provider)
    if len(hour_map) == 24:
        vals = list(hour_map.values())
        if any(v == "FAILED" for v in vals):
            day_status = "FAILED"
        elif all(v == "EXPECTED_EMPTY" for v in vals):
            day_status = "EXPECTED_EMPTY"
        elif any(v == "SUCCESS" for v in vals):
            day_status = "SUCCESS"
        else:
            day_status = "MISSING"
    elif any(v == "FAILED" for v in hour_map.values()):
        day_status = "FAILED"
    else:
        day_status = "INCOMPLETE"

    try:
        tick_count = write_ticks_parquet(tpath, merged)
        reloaded = read_ticks_parquet(tpath) if tpath.exists() else []
        if tick_count > 0:
            tick_status = "SUCCESS"
        elif day_status == "EXPECTED_EMPTY":
            tick_status = "EXPECTED_EMPTY"
        elif day_status == "INCOMPLETE":
            tick_status = "INCOMPLETE"
        else:
            tick_status = day_status

        bars = ticks_to_seconds(reloaded, 1)
        bars_count = write_bars_parquet(bpath, bars)
        _ = read_bars_parquet(bpath) if bpath.exists() and bpath.stat().st_size > 0 else []
        if bars_count > 0:
            bars_status = "SUCCESS"
        elif tick_count == 0 and day_status == "EXPECTED_EMPTY":
            bars_status = "EXPECTED_EMPTY"
        elif day_status == "INCOMPLETE":
            bars_status = "INCOMPLETE"
        else:
            bars_status = day_status
    except Exception as e:  # noqa: BLE001
        error = str(e)
        tick_status = tick_status if tick_count else "FAILED"
        bars_status = "FAILED"
        day_status = "FAILED"

    if day_status == "FAILED" or tick_status == "FAILED" or bars_status == "FAILED":
        final = "FAILED"
    elif day_status == "INCOMPLETE" or tick_status == "INCOMPLETE" or bars_status == "INCOMPLETE":
        final = "INCOMPLETE"
    elif day_status == "EXPECTED_EMPTY" and tick_count == 0:
        final = "EXPECTED_EMPTY"
    elif tick_status == "SUCCESS" and bars_status == "SUCCESS":
        final = "SUCCESS"
    else:
        final = day_status

    ok_h = sum(1 for v in hour_map.values() if v == "SUCCESS")
    empty_h = sum(1 for v in hour_map.values() if v == "EXPECTED_EMPTY")
    fail_h = sum(1 for v in hour_map.values() if v == "FAILED")

    meta = {
        "symbol": sym,
        "provider": provider,
        "day": day.isoformat(),
        "status": final,
        "tick_status": tick_status,
        "bars_1s_status": bars_status,
        "tick_count": tick_count,
        "bars_1s_count": bars_count,
        "successful_hours": ok_h,
        "empty_hours": empty_h,
        "failed_hours": fail_h,
        "ohlc_price": "mid=(bid+ask)/2",
        "tick_schema": ["time", "bid", "ask", "bid_vol", "ask_vol", "mid"],
        "bars_1s_schema": ["time", "open", "high", "low", "close", "volume"],
        "hour_classifications": {f"{h:02d}": hour_map.get(h) for h in range(24)},
        "error": error,
    }
    write_metadata(mpath, meta)

    upsert_day(
        db,
        {
            "symbol": sym,
            "provider": provider,
            "day": day.isoformat(),
            "status": final,
            "tick_status": tick_status,
            "bars_1s_status": bars_status,
            "tick_count": tick_count,
            "bars_1s_count": bars_count,
            "successful_hours": ok_h,
            "empty_hours": empty_h,
            "failed_hours": fail_h,
            "tick_file_path": str(tpath),
            "bars_1s_file_path": str(bpath),
            "metadata_path": str(mpath),
            "error": error,
        },
    )


    # Durable Telegram archive (optional; disabled unless TELEGRAM_ARCHIVE_ENABLED)
    archive_info = None
    if final in ("SUCCESS", "EXPECTED_EMPTY"):
        try:
            from bot.archive import enqueue_archive_for_day
            from bot.config import archive_enabled

            if archive_enabled():
                archive_info = enqueue_archive_for_day(
                    db_path if isinstance(db_path, Path) else Path(str(db_path)),
                    sym,
                    day.isoformat(),
                    provider=provider,
                    root=data_root,
                )
        except Exception as e:  # noqa: BLE001
            log.warning("archive enqueue failed for %s %s: %s", sym, day, e)
            archive_info = {"error": str(e)}

    result.update(
        {
            "status": final,
            "tick_status": tick_status,
            "bars_1s_status": bars_status,
            "tick_count": tick_count,
            "bars_1s_count": bars_count,
            "successful_hours": ok_h,
            "empty_hours": empty_h,
            "failed_hours": fail_h,
            "hours_fetched": need_hours,
            "tick_file_path": str(tpath),
            "bars_1s_file_path": str(bpath),
            "metadata_path": str(mpath),
            "tick_file_bytes": tpath.stat().st_size if tpath.exists() else 0,
            "bars_file_bytes": bpath.stat().st_size if bpath.exists() else 0,
            "error": error,
            "channel_archive": archive_info,
        }
    )
    return result


def _try_archive(
    db_path: Path,
    symbol: str,
    day: str,
    provider: str,
    paths: dict[str, Any],
) -> dict[str, Any]:
    """Best-effort upload to Telegram storage channel."""
    try:
        from bot.channel_storage import upload_day_to_channel_sync
        from pathlib import Path as P

        return upload_day_to_channel_sync(
            db_path=db_path,
            symbol=symbol,
            day=day,
            tick_path=P(paths["tick_file_path"]) if paths.get("tick_file_path") else None,
            bars_path=P(paths["bars_1s_file_path"]) if paths.get("bars_1s_file_path") else None,
            metadata_path=P(paths["metadata_path"]) if paths.get("metadata_path") else None,
            provider=provider,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("channel archive failed for %s %s", symbol, day)
        return {"error": str(e)}


def _merge_ticks(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Idempotent merge: unique by (time, bid, ask)."""
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for t in list(existing) + list(new):
        key = (round(float(t["time"]), 6), round(float(t.get("bid") or 0), 8), round(float(t.get("ask") or 0), 8))
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    out.sort(key=lambda x: x["time"])
    return out
