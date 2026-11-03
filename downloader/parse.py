"""Dukascopy .bi5 decompress + 20-byte tick parse — ported from Trading Replay."""

from __future__ import annotations

import gzip
import lzma
import struct
from typing import Any

RECORD_SIZE = 20


def decompress(content: bytes) -> bytes:
    if not content:
        return b""
    for decoder in (lzma.decompress, gzip.decompress):
        try:
            return decoder(content)
        except Exception:
            continue
    if len(content) % RECORD_SIZE == 0:
        return content
    raise ValueError("Unsupported or corrupt Dukascopy compression")


def parse_ticks(raw: bytes, hour_start: float, divisor: float) -> list[dict[str, Any]]:
    """Parse binary tick records.

    Schema per tick:
      time: float seconds since epoch (ms precision from source)
      bid, ask: float
      bid_vol, ask_vol: float
      mid: (bid+ask)/2 when both positive
    """
    out: list[dict[str, Any]] = []
    for i in range(0, len(raw) - (RECORD_SIZE - 1), RECORD_SIZE):
        chunk = raw[i : i + RECORD_SIZE]
        try:
            ms, ask_i, bid_i, ask_vol, bid_vol = struct.unpack(">IIIff", chunk)
        except struct.error:
            continue
        ask = ask_i / divisor
        bid = bid_i / divisor
        if ask <= 0 and bid <= 0:
            continue
        bid_f = bid if bid > 0 else ask
        ask_f = ask if ask > 0 else bid
        out.append(
            {
                "time": hour_start + ms / 1000.0,
                "bid": bid_f,
                "ask": ask_f,
                "bid_vol": float(bid_vol),
                "ask_vol": float(ask_vol),
                "mid": (bid_f + ask_f) / 2.0,
            }
        )
    return out
