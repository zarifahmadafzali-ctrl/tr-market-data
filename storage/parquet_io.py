"""Parquet read/write for ticks and 1s bars."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

TICK_COLUMNS = ["time", "bid", "ask", "bid_vol", "ask_vol", "mid"]
BAR_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _write_df(path: Path, df: pd.DataFrame) -> None:
    """Write via temp file then replace (some environments fail writing Parquet into paths with '=')."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        df.to_parquet(tmp_path, index=False, compression="snappy")
        shutil.copyfile(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_ticks_parquet(path: Path, ticks: list[dict[str, Any]]) -> int:
    if not ticks:
        df = pd.DataFrame(columns=TICK_COLUMNS)
    else:
        df = pd.DataFrame(ticks)
        for c in TICK_COLUMNS:
            if c not in df.columns:
                df[c] = 0.0
        df = df[TICK_COLUMNS].sort_values("time").reset_index(drop=True)
    _write_df(path, df)
    return len(df)


def read_ticks_parquet(path: Path) -> list[dict[str, Any]]:
    df = pd.read_parquet(path)
    return df.to_dict(orient="records")


def write_bars_parquet(path: Path, bars: list[dict[str, Any]]) -> int:
    if not bars:
        df = pd.DataFrame(columns=BAR_COLUMNS)
    else:
        df = pd.DataFrame(bars)
        for c in BAR_COLUMNS:
            if c not in df.columns:
                df[c] = 0.0
        df = df[BAR_COLUMNS].sort_values("time").reset_index(drop=True)
    _write_df(path, df)
    return len(df)


def read_bars_parquet(path: Path) -> list[dict[str, Any]]:
    df = pd.read_parquet(path)
    return df.to_dict(orient="records")


def write_metadata(path: Path, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
