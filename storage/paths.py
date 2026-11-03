"""Hive-style local paths for durable market data files."""

from __future__ import annotations

from datetime import date
from pathlib import Path


def day_dir(
    root: Path,
    *,
    provider: str,
    symbol: str,
    day: date,
) -> Path:
    return (
        root
        / f"provider={provider}"
        / f"symbol={symbol.upper()}"
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
    )


def ticks_parquet_path(day_path: Path) -> Path:
    return day_path / "ticks.parquet"


def bars_1s_parquet_path(day_path: Path) -> Path:
    return day_path / "bars_1s.parquet"


def metadata_json_path(day_path: Path) -> Path:
    return day_path / "metadata.json"
