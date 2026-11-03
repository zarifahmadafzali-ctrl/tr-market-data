from datetime import date
from pathlib import Path

from db.catalog import connect, get_day, upsert_day
from processor.ohlc import ticks_to_seconds
from storage.parquet_io import read_bars_parquet, read_ticks_parquet, write_bars_parquet, write_ticks_parquet
from storage.paths import day_dir


def test_tick_and_bar_parquet_roundtrip(tmp_path: Path):
    ticks = [
        {"time": 1000.001, "bid": 10.0, "ask": 10.2, "bid_vol": 1.5, "ask_vol": 2.5, "mid": 10.1},
        {"time": 1000.500, "bid": 10.1, "ask": 10.3, "bid_vol": 1.0, "ask_vol": 1.0, "mid": 10.2},
        {"time": 1001.000, "bid": 9.9, "ask": 10.1, "bid_vol": 0.5, "ask_vol": 0.5, "mid": 10.0},
    ]
    d = day_dir(tmp_path, provider="dukascopy", symbol="USA30IDXUSD", day=date(2024, 6, 12))
    tpath = d / "ticks.parquet"
    bpath = d / "bars_1s.parquet"
    n = write_ticks_parquet(tpath, ticks)
    assert n == 3
    reloaded = read_ticks_parquet(tpath)
    assert len(reloaded) == 3
    assert reloaded[0]["time"] <= reloaded[1]["time"]
    assert abs(reloaded[0]["bid"] - 10.0) < 1e-9
    assert abs(reloaded[0]["ask"] - 10.2) < 1e-9
    bars = ticks_to_seconds(reloaded, 1)
    write_bars_parquet(bpath, bars)
    bars2 = read_bars_parquet(bpath)
    assert len(bars2) == len(bars)
    assert bars2[0]["open"] == bars[0]["open"]


def test_catalog_status(tmp_path: Path):
    db = connect(tmp_path / "t.sqlite")
    upsert_day(
        db,
        {
            "symbol": "USA30IDXUSD",
            "provider": "dukascopy",
            "day": "2024-06-12",
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 10,
            "bars_1s_count": 5,
            "successful_hours": 20,
            "empty_hours": 4,
            "failed_hours": 0,
            "tick_file_path": "x",
            "bars_1s_file_path": "y",
        },
    )
    row = get_day(db, "USA30IDXUSD", "2024-06-12")
    assert row is not None
    assert row["status"] == "SUCCESS"
    assert row["tick_count"] == 10
