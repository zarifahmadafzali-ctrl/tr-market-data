"""Parse /get and related user input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from downloader.symbols import ALIASES, normalize_symbol

Kind = Literal["tick", "1s", "both"]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class GetRequest:
    symbol: str
    start: date
    end: date
    kind: Kind


def parse_date(s: str) -> date:
    s = s.strip()
    if not _DATE_RE.match(s):
        raise ValueError(f"Invalid date '{s}'. Use YYYY-MM-DD.")
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"Invalid date '{s}'.") from e


def parse_kind(s: str) -> Kind:
    k = s.strip().lower()
    if k in ("tick", "ticks", "raw"):
        return "tick"
    if k in ("1s", "bars", "ohlc", "bar"):
        return "1s"
    if k in ("both", "all"):
        return "both"
    raise ValueError("Kind must be tick, 1s, or both.")


def parse_get_args(args: list[str]) -> GetRequest:
    """
    Forms:
      US30 2024-06-12 tick
      US30 2024-06-12 1s
      US30 2024-06-10 2024-06-12 both
      US30 2024-06-12          -> both, single day
    """
    if len(args) < 2:
        raise ValueError("Usage: /get SYMBOL YYYY-MM-DD [YYYY-MM-DD] [tick|1s|both]")

    symbol = normalize_symbol(args[0])
    start = parse_date(args[1])
    end = start
    kind: Kind = "both"
    rest = args[2:]

    if rest:
        if _DATE_RE.match(rest[0]):
            end = parse_date(rest[0])
            rest = rest[1:]
        if rest:
            kind = parse_kind(rest[0])
            rest = rest[1:]
        if rest:
            raise ValueError("Too many arguments.")

    if end < start:
        raise ValueError("End date must be on or after start date.")
    # Guardrails
    if (end - start).days > 366:
        raise ValueError("Date range cannot exceed 366 days.")

    return GetRequest(symbol=symbol, start=start, end=end, kind=kind)


def supported_symbols() -> list[str]:
    # Friendly aliases + normalized ids
    return sorted(set(list(ALIASES.keys()) + list(ALIASES.values())))
