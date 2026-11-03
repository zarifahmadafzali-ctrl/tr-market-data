"""Local filesystem layout for durable market-data files."""

from __future__ import annotations

from pathlib import Path


def ensure_layout(root: Path) -> dict[str, Path]:
    raw = root / "raw"
    processed = root / "processed"
    cache = root / "cache"
    for p in (raw, processed, cache):
        p.mkdir(parents=True, exist_ok=True)
    return {"root": root, "raw": raw, "processed": processed, "cache": cache}
