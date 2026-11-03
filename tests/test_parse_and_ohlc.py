import gzip
import lzma
import struct

from downloader.parse import RECORD_SIZE, decompress, parse_ticks
from processor.ohlc import ticks_to_seconds


def _pack_tick(ms: int, ask_i: int, bid_i: int, ask_vol: float = 1.0, bid_vol: float = 1.0) -> bytes:
    return struct.pack(">IIIff", ms, ask_i, bid_i, ask_vol, bid_vol)


def test_lzma_roundtrip_and_parse():
    # mid prices around 39000 with divisor 1000 → ints 39000000
    raw = b"".join(
        [
            _pack_tick(0, 39_000_000, 38_999_000),
            _pack_tick(500, 39_001_000, 39_000_000),
            _pack_tick(1500, 39_002_000, 39_001_000),
        ]
    )
    compressed = lzma.compress(raw)
    decoded = decompress(compressed)
    assert len(decoded) == 3 * RECORD_SIZE
    ticks = parse_ticks(decoded, hour_start=1_000_000.0, divisor=1000.0)
    assert len(ticks) == 3
    assert ticks[0]["time"] == 1_000_000.0
    assert ticks[1]["time"] == 1_000_000.5
    assert abs(ticks[0]["ask"] - 39000.0) < 1e-6
    assert abs(ticks[0]["bid"] - 38999.0) < 1e-6
    assert ticks[0]["bid_vol"] == 1.0 and ticks[0]["ask_vol"] == 1.0
    assert ticks[0]["time"] <= ticks[1]["time"] <= ticks[2]["time"]


def test_gzip_decompress():
    raw = _pack_tick(0, 1000, 1000)
    decoded = decompress(gzip.compress(raw))
    assert len(decoded) == RECORD_SIZE


def test_ohlc_mid_no_fabricate():
    ticks = [
        {"time": 100.0, "bid": 10.0, "ask": 12.0, "bid_vol": 1, "ask_vol": 1, "mid": 11.0},
        {"time": 100.4, "bid": 11.0, "ask": 13.0, "bid_vol": 1, "ask_vol": 1, "mid": 12.0},
        {"time": 102.1, "bid": 9.0, "ask": 11.0, "bid_vol": 1, "ask_vol": 1, "mid": 10.0},
    ]
    bars = ticks_to_seconds(ticks, 1)
    # Only seconds 100 and 102 — no bar for 101
    assert [b["time"] for b in bars] == [100, 102]
    assert bars[0]["open"] == 11.0
    assert bars[0]["high"] == 12.0
    assert bars[0]["low"] == 11.0
    assert bars[0]["close"] == 12.0


def test_ohlc_ordering_shuffled_input():
    ticks = [
        {"time": 5.2, "bid": 1, "ask": 1, "mid": 2.0, "bid_vol": 0, "ask_vol": 0},
        {"time": 4.1, "bid": 1, "ask": 1, "mid": 1.0, "bid_vol": 0, "ask_vol": 0},
    ]
    bars = ticks_to_seconds(ticks, 1)
    assert bars[0]["time"] == 4
    assert bars[0]["open"] == 1.0
