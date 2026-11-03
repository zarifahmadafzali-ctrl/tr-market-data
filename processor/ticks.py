"""Tick decode + aggregation placeholders (pilot).

Dukascopy .bi5 is LZMA-compressed binary ticks. Full decoder lands in the next
iteration after the pilot hour-download path is verified.
"""

from __future__ import annotations

from pathlib import Path


def list_bi5_files(day_dir: Path) -> list[Path]:
    if not day_dir.is_dir():
        return []
    return sorted(day_dir.glob("*h_ticks.bi5"))
