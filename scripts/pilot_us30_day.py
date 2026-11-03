#!/usr/bin/env python3
"""Pilot: download one calendar day of US30 hourly tick files from Dukascopy."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.catalog import connect, set_day_status, upsert_symbol
from downloader.dukascopy import download_day_hours
from storage.local import ensure_layout

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("pilot_us30")


async def amain(day: date) -> int:
    paths = ensure_layout(ROOT / "data")
    db = connect(ROOT / "db" / "tr_market_data.sqlite")
    upsert_symbol(db, "US30", "US30IDXUSD", "US30 index CFD (Dukascopy)")
    set_day_status(db, "US30", day.isoformat(), "running")
    log.info("Downloading US30 %s …", day.isoformat())
    result = await download_day_hours("US30", day, paths["raw"])
    status = "success" if result["failed"] == 0 and result["ok"] > 0 else (
        "expected_empty" if result["ok"] == 0 and result["failed"] == 0 else "failed"
    )
    set_day_status(
        db,
        "US30",
        day.isoformat(),
        status,
        ticks=0,
        raw_path=str(paths["raw"] / "US30" / day.isoformat()),
        error=None if status != "failed" else f"failed_hours={result['failed']}",
    )
    print(json.dumps(result, indent=2))
    return 0 if status != "failed" else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Pilot US30 day download")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = p.parse_args()
    day = date.fromisoformat(args.date)
    raise SystemExit(asyncio.run(amain(day)))


if __name__ == "__main__":
    main()
