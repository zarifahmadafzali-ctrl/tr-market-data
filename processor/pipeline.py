"""Day pipeline: download → ticks parquet → 1s bars parquet → catalog."""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path
from typing import Any

from db.catalog import connect, upsert_day, upsert_symbol
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
) -> dict[str, Any]:
    sym = normalize_symbol(symbol)
    db = connect(db_path)
    upsert_symbol(db, sym, sym, description=f"{symbol} via Dukascopy")

    result = {
        "symbol": sym,
        "day": day.isoformat(),
        "provider": provider,
        "status": "RUNNING",
    }

    try:
        downloaded = asyncio.run(download_day(symbol, day))  # uses aliases inside
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

    ticks = downloaded["ticks"]
    day_status = downloaded["status"]
    dpath = day_dir(data_root, provider=provider, symbol=sym, day=day)
    tpath = ticks_parquet_path(dpath)
    bpath = bars_1s_parquet_path(dpath)
    mpath = metadata_json_path(dpath)

    tick_status = "FAILED"
    bars_status = "FAILED"
    tick_count = 0
    bars_count = 0
    error: str | None = None

    try:
        tick_count = write_ticks_parquet(tpath, ticks)
        # Reload to prove round-trip (and use reloaded for OHLC)
        reloaded = read_ticks_parquet(tpath) if tpath.exists() else []
        tick_status = (
            "SUCCESS"
            if tick_count > 0
            else ("EXPECTED_EMPTY" if day_status == "EXPECTED_EMPTY" else day_status)
        )
        if day_status == "FAILED":
            tick_status = "FAILED"

        bars = ticks_to_seconds(reloaded, 1)
        bars_count = write_bars_parquet(bpath, bars)
        _ = read_bars_parquet(bpath) if bpath.exists() else []
        bars_status = (
            "SUCCESS"
            if bars_count > 0
            else ("EXPECTED_EMPTY" if tick_count == 0 and day_status != "FAILED" else day_status)
        )
        if day_status == "FAILED":
            bars_status = "FAILED"
    except Exception as e:  # noqa: BLE001
        error = str(e)
        tick_status = tick_status if tick_count else "FAILED"
        bars_status = "FAILED"
        day_status = "FAILED"

    # Full day success only if download ok AND both outputs ok when ticks expected
    if day_status == "FAILED" or tick_status == "FAILED" or bars_status == "FAILED":
        final = "FAILED"
    elif day_status == "EXPECTED_EMPTY" and tick_count == 0:
        final = "EXPECTED_EMPTY"
    elif tick_status == "SUCCESS" and bars_status == "SUCCESS":
        final = "SUCCESS"
    else:
        final = day_status

    meta = {
        "symbol": sym,
        "provider": provider,
        "day": day.isoformat(),
        "status": final,
        "tick_status": tick_status,
        "bars_1s_status": bars_status,
        "tick_count": tick_count,
        "bars_1s_count": bars_count,
        "successful_hours": downloaded["successful_hours"],
        "empty_hours": downloaded["empty_hours"],
        "failed_hours": downloaded["failed_hours"],
        "ohlc_price": "mid=(bid+ask)/2",
        "tick_schema": ["time", "bid", "ask", "bid_vol", "ask_vol", "mid"],
        "bars_1s_schema": ["time", "open", "high", "low", "close", "volume"],
        "hour_classifications": {
            f"{h['hour']:02d}": h["classification"] for h in downloaded["hours"]
        },
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
            "successful_hours": downloaded["successful_hours"],
            "empty_hours": downloaded["empty_hours"],
            "failed_hours": downloaded["failed_hours"],
            "tick_file_path": str(tpath),
            "bars_1s_file_path": str(bpath),
            "metadata_path": str(mpath),
            "error": error,
        },
    )

    result.update(
        {
            "status": final,
            "tick_status": tick_status,
            "bars_1s_status": bars_status,
            "tick_count": tick_count,
            "bars_1s_count": bars_count,
            "successful_hours": downloaded["successful_hours"],
            "empty_hours": downloaded["empty_hours"],
            "failed_hours": downloaded["failed_hours"],
            "tick_file_path": str(tpath),
            "bars_1s_file_path": str(bpath),
            "metadata_path": str(mpath),
            "tick_file_bytes": tpath.stat().st_size if tpath.exists() else 0,
            "bars_file_bytes": bpath.stat().st_size if bpath.exists() else 0,
            "hour_classifications": meta["hour_classifications"],
            "error": error,
        }
    )
    return result
