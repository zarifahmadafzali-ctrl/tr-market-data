"""Phase 3B read-only validation tests (temp SQLite + temp data root)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("pyarrow")

from db.catalog import connect, upsert_day, upsert_hour
from processor.ohlc import ticks_to_seconds
from storage.parquet_io import write_bars_parquet, write_metadata, write_ticks_parquet
from storage.paths import bars_1s_parquet_path, day_dir, metadata_json_path, ticks_parquet_path
from validator.day_validate import scan_orphans, validate_day


def _seed_ok(tmp: Path, *, bad_open: bool = False, wrong_count: bool = False):
    db = tmp / "c.sqlite"
    root = tmp / "data"
    sym = "USA30IDXUSD"
    day = date(2024, 6, 12)
    d = day_dir(root, provider="dukascopy", symbol=sym, day=day)
    ticks = [
        {"time": 1_000.001, "bid": 10.0, "ask": 10.2, "bid_vol": 1.0, "ask_vol": 1.0, "mid": 10.1},
        # same millisecond, different price — must NOT be an error by itself
        {"time": 1_000.001, "bid": 10.05, "ask": 10.25, "bid_vol": 1.0, "ask_vol": 1.0, "mid": 10.15},
        {"time": 1_001.000, "bid": 10.1, "ask": 10.3, "bid_vol": 1.0, "ask_vol": 1.0, "mid": 10.2},
    ]
    bars = ticks_to_seconds(ticks, 1)
    if bad_open and bars:
        bars = [dict(bars[0], open=bars[0]["high"] + 1.0), *bars[1:]]
    tpath = ticks_parquet_path(d)
    bpath = bars_1s_parquet_path(d)
    mpath = metadata_json_path(d)
    write_ticks_parquet(tpath, ticks)
    write_bars_parquet(bpath, bars)
    write_metadata(mpath, {"symbol": sym, "day": day.isoformat()})
    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": sym,
            "provider": "dukascopy",
            "day": day.isoformat(),
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 999 if wrong_count else len(ticks),
            "bars_1s_count": len(bars),
            "successful_hours": 1,
            "empty_hours": 0,
            "failed_hours": 0,
            "tick_file_path": str(tpath.relative_to(root)),
            "bars_1s_file_path": str(bpath.relative_to(root)),
            "metadata_path": str(mpath.relative_to(root)),
        },
    )
    upsert_hour(
        conn,
        {
            "symbol": sym,
            "provider": "dukascopy",
            "day": day.isoformat(),
            "hour": 0,
            "status": "SUCCESS",
            "tick_count": len(ticks),
        },
    )
    return db, root, sym, day.isoformat()


def test_validate_ok_allows_same_ms_ticks(tmp_path: Path):
    db, root, sym, day = _seed_ok(tmp_path)
    rep = validate_day(db_path=db, data_root=root, symbol="US30", day=day)
    assert rep.ok, rep.to_dict()
    codes = {f.code for f in rep.findings}
    assert "DUPLICATE_TICK_RECORDS" not in codes


def test_validate_ohlc_open_out_of_range(tmp_path: Path):
    db, root, sym, day = _seed_ok(tmp_path, bad_open=True)
    rep = validate_day(db_path=db, data_root=root, symbol=sym, day=day)
    assert not rep.ok
    assert any(f.code == "OHLC_OPEN_OUT_OF_RANGE" for f in rep.findings if f.severity == "error")


def test_validate_count_mismatch_is_error(tmp_path: Path):
    db, root, sym, day = _seed_ok(tmp_path, wrong_count=True)
    rep = validate_day(db_path=db, data_root=root, symbol=sym, day=day)
    assert not rep.ok
    assert any(f.code == "TICK_COUNT_MISMATCH" and f.severity == "error" for f in rep.findings)


def test_missing_file_is_error(tmp_path: Path):
    db = tmp_path / "c.sqlite"
    root = tmp_path / "data"
    root.mkdir()
    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": "USA30IDXUSD",
            "provider": "dukascopy",
            "day": "2024-06-12",
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 1,
            "bars_1s_count": 1,
            "tick_file_path": "missing/ticks.parquet",
            "bars_1s_file_path": "missing/bars_1s.parquet",
        },
    )
    rep = validate_day(db_path=db, data_root=root, symbol="US30", day="2024-06-12")
    assert not rep.ok
    codes = {f.code for f in rep.findings if f.severity == "error"}
    assert "MISSING_TICK_FILE" in codes or "MISSING_BARS_FILE" in codes


def test_orphan_is_warning_only(tmp_path: Path):
    db = tmp_path / "c.sqlite"
    root = tmp_path / "data"
    connect(db)  # empty schema
    d = day_dir(root, provider="dukascopy", symbol="USA30IDXUSD", day=date(2024, 1, 2))
    write_ticks_parquet(ticks_parquet_path(d), [])
    findings = scan_orphans(data_root=root, db_path=db)
    assert findings
    assert all(f.severity == "warning" for f in findings)
    assert any(f.code == "ORPHAN_FILE" for f in findings)


def test_bid_gt_ask_error(tmp_path: Path):
    db = tmp_path / "c.sqlite"
    root = tmp_path / "data"
    sym = "USA30IDXUSD"
    day = date(2024, 6, 12)
    d = day_dir(root, provider="dukascopy", symbol=sym, day=day)
    ticks = [{"time": 1.0, "bid": 11.0, "ask": 10.0, "bid_vol": 1, "ask_vol": 1, "mid": 10.5}]
    bars = ticks_to_seconds(ticks, 1)
    write_ticks_parquet(ticks_parquet_path(d), ticks)
    write_bars_parquet(bars_1s_parquet_path(d), bars)
    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": sym,
            "provider": "dukascopy",
            "day": day.isoformat(),
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 1,
            "bars_1s_count": len(bars),
            "tick_file_path": str(ticks_parquet_path(d).relative_to(root)),
            "bars_1s_file_path": str(bars_1s_parquet_path(d).relative_to(root)),
        },
    )
    rep = validate_day(db_path=db, data_root=root, symbol=sym, day=day.isoformat())
    assert not rep.ok
    assert any(f.code == "BID_GT_ASK" for f in rep.findings)
