"""CLI: read-only validation of catalog vs Parquet files.

Examples:
  python -m scripts.validate_data --symbol US30 --day 2024-06-12
  python -m scripts.validate_data --all-success
  python -m scripts.validate_data --scan-orphans
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import data_root, sqlite_path
from validator.day_validate import scan_orphans, validate_day, validate_success_days


def _print_day(rep) -> None:
    d = rep.to_dict()
    status = "OK" if rep.ok else "FAIL"
    print(f"[{status}] {rep.symbol} {rep.day}")
    if d.get("stats"):
        print(f"  stats: {json.dumps(d['stats'], default=str)}")
    for e in d["errors"]:
        print(f"  ERROR  {e['code']}: {e['message']}")
    for w in d["warnings"]:
        print(f"  WARN   {w['code']}: {w['message']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only TR Market Data validation")
    ap.add_argument("--symbol", help="Symbol e.g. US30")
    ap.add_argument("--day", help="YYYY-MM-DD")
    ap.add_argument("--all-success", action="store_true", help="Validate every SUCCESS day in catalog")
    ap.add_argument("--scan-orphans", action="store_true", help="Warn on Parquet without catalog row")
    ap.add_argument("--provider", default="dukascopy")
    ap.add_argument("--db", type=Path, default=None, help="Override SQLITE_PATH")
    ap.add_argument("--data-root", type=Path, default=None, help="Override DATA_ROOT")
    ap.add_argument("--json", type=Path, default=None, help="Write full JSON report")
    args = ap.parse_args(argv)

    db = args.db or sqlite_path()
    root = args.data_root or data_root()

    reports = []
    orphan_findings = []

    if args.all_success:
        reports = validate_success_days(db_path=db, data_root=root, provider=args.provider)
        for r in reports:
            _print_day(r)
    elif args.symbol and args.day:
        r = validate_day(
            db_path=db,
            data_root=root,
            symbol=args.symbol,
            day=args.day,
            provider=args.provider,
        )
        reports = [r]
        _print_day(r)
    elif not args.scan_orphans:
        ap.error("Use --symbol and --day, or --all-success, and/or --scan-orphans")

    if args.scan_orphans:
        orphan_findings = scan_orphans(data_root=root, db_path=db, provider=args.provider)
        print(f"Orphan scan: {len(orphan_findings)} warning(s)")
        for f in orphan_findings:
            print(f"  WARN   {f.code}: {f.message}")

    payload = {
        "db": str(db),
        "data_root": str(root),
        "days": [r.to_dict() for r in reports],
        "orphans": [
            {"code": f.code, "severity": f.severity, "message": f.message, "context": f.context}
            for f in orphan_findings
        ],
    }
    if args.json:
        args.json.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Wrote {args.json}")

    has_error = any(not r.ok for r in reports)
    return 1 if has_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
