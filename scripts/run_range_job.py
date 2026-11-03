#!/usr/bin/env python3
"""Enqueue and run a multi-day download job."""

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

from scheduler.jobs import enqueue_range_job, run_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", action="append", required=True, help="Repeatable, e.g. --symbol US30")
    p.add_argument("--start", required=True, help="YYYY-MM-DD")
    p.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = p.parse_args()
    db = ROOT / "db" / "tr_market_data.sqlite"
    data = ROOT / "data"
    job_id = enqueue_range_job(
        db,
        symbols=args.symbol,
        start_day=date.fromisoformat(args.start),
        end_day=date.fromisoformat(args.end),
    )
    print(json.dumps({"enqueued_job_id": job_id}))
    out = run_job(db, job_id, data_root=data)
    print(json.dumps(out, indent=2, default=str))
    raise SystemExit(0 if out.get("status") in ("COMPLETED", "PAUSED") else 1)


if __name__ == "__main__":
    main()
