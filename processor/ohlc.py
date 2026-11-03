"""Tick → OHLC aggregation.

Compatibility with Trading Replay: 1-second bars use mid-price
(mid = (bid+ask)/2). Raw bid/ask ticks are stored separately and are not discarded.
Seconds without any tick produce no bar (no fabrication).
"""

from __future__ import annotations

from typing import Any


def ticks_to_seconds(tick_list: list[dict[str, Any]], seconds: int = 1) -> list[dict[str, Any]]:
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    if not tick_list:
        return []

    ordered = sorted(tick_list, key=lambda x: x["time"])
    buckets: dict[int, list[float]] = {}
    for t in ordered:
        ts = float(t["time"])
        bucket = int(ts // seconds) * seconds
        if "mid" in t and t["mid"] is not None:
            mid = float(t["mid"])
        else:
            bid = float(t.get("bid") or 0)
            ask = float(t.get("ask") or 0)
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
            else:
                mid = bid or ask
        vol = float(t.get("bid_vol") or 0) + float(t.get("ask_vol") or 0) + float(t.get("volume") or 0)
        if bucket not in buckets:
            buckets[bucket] = [mid, mid, mid, mid, vol]
        else:
            b = buckets[bucket]
            b[1] = max(b[1], mid)
            b[2] = min(b[2], mid)
            b[3] = mid
            b[4] += vol

    return [
        {
            "time": k,
            "open": b[0],
            "high": b[1],
            "low": b[2],
            "close": b[3],
            "volume": b[4],
        }
        for k, b in sorted(buckets.items())
    ]
