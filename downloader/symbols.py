"""Symbol normalization — ported from Trading Replay dukascopy provider."""

from __future__ import annotations

# Dukascopy symbols whose published integer prices use 1/1000 points.
DIV_1000 = {
    "USA30IDXUSD",
    "USA500IDXUSD",
    "USATECHIDXUSD",
    "DEUIDXEUR",
    "XAUUSD",
    "XAGUSD",
    "BRENTCMDUSD",
    "LIGHTCMDUSD",
}

ALIASES = {
    "US30": "USA30IDXUSD",
    "USA30": "USA30IDXUSD",
    "DJ30": "USA30IDXUSD",
    "DJIA": "USA30IDXUSD",
    "GOLD": "XAUUSD",
    "XAUUSD": "XAUUSD",
    "BTCUSD": "BTCUSD",
    "EURUSD": "EURUSD",
}


def normalize_symbol(symbol: str) -> str:
    sym = symbol.replace("/", "").replace(".", "").replace(" ", "").upper()
    return ALIASES.get(sym, sym)


def point_divisor(sym: str) -> float:
    return 1000.0 if sym in DIV_1000 else 100_000.0
