#!/usr/bin/env python3
"""Pilot: full US30 day → ticks.parquet + bars_1s.parquet + SQLite catalog."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from processor.pipeline import run_day_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = p.parse_args()
    day = date.fromisoformat(args.date)
    out = run_day_pipeline(
        "US30",
        day,
        data_root=ROOT / "data",
        db_path=ROOT / "db" / "tr_market_data.sqlite",
    )
    print(json.dumps(out, indent=2))
    raise SystemExit(0 if out.get("status") != "FAILED" else 1)


if __name__ == "__main__":
    main()
