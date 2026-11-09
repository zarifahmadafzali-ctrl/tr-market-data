"""Dukascopy historical tick hour downloader with 429 backoff."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

import httpx

from downloader.parse import decompress, parse_ticks
from downloader.symbols import normalize_symbol, point_divisor

log = logging.getLogger(__name__)

BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_429_BACKOFF = (2.0, 5.0, 10.0, 20.0)
_MAX_RETRY_AFTER = 60.0
_MAX_429_ATTEMPTS = 5
_MAX_OTHER_ATTEMPTS = 3
_INTER_REQUEST_DELAY = 0.35


def hour_url(dukascopy_id: str, day: date, hour: int) -> str:
    # Zero-based month in Dukascopy historical paths (June → 05).
    return (
        f"{BASE_URL}/{dukascopy_id}/{day.year}/{day.month - 1:02d}/"
        f"{day.day:02d}/{hour:02d}h_ticks.bi5"
    )


def hour_start_unix(day: date, hour: int) -> float:
    dt = datetime(day.year, day.month, day.day, hour, tzinfo=timezone.utc)
    return dt.timestamp()


async def download_hour_ticks(
    symbol: str,
    day: date,
    hour: int,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Download and parse one hour of ticks.

    Returns classification SUCCESS | EXPECTED_EMPTY | FAILED plus ticks list.
    """
    sym = normalize_symbol(symbol)
    url = hour_url(sym, day, hour)
    divisor = point_divisor(sym)
    h0 = hour_start_unix(day, hour)
    last_error = ""
    last_status = 0
    attempt = 0
    max_attempts = _MAX_429_ATTEMPTS

    while attempt < max_attempts:
        attempt += 1
        try:
            if attempt > 1 or hour > 0:
                await asyncio.sleep(_INTER_REQUEST_DELAY)
            resp = await client.get(url, timeout=60.0)
            last_status = resp.status_code

            if resp.status_code in (404, 204):
                return {
                    "hour": hour,
                    "ticks": [],
                    "status": resp.status_code,
                    "classification": "EXPECTED_EMPTY",
                    "error": "no file",
                    "attempts": attempt,
                    "url": url,
                }

            if resp.status_code == 429:
                last_error = "HTTP 429 Too Many Requests"
                if attempt >= _MAX_429_ATTEMPTS:
                    break
                delay = _429_BACKOFF[min(attempt - 1, len(_429_BACKOFF) - 1)]
                ra = resp.headers.get("Retry-After")
                if ra:
                    try:
                        delay = max(delay, min(_MAX_RETRY_AFTER, float(ra)))
                    except ValueError:
                        pass
                log.warning("429 %s — sleep %.1fs (attempt %s)", url, delay, attempt)
                await asyncio.sleep(delay)
                continue

            # Transient upstream errors (common on Dukascopy under load)
            if resp.status_code in (502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
                if attempt >= _MAX_429_ATTEMPTS:
                    break
                delay = _429_BACKOFF[min(attempt - 1, len(_429_BACKOFF) - 1)]
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 400:
                last_error = f"HTTP {resp.status_code}"
                if attempt >= _MAX_OTHER_ATTEMPTS:
                    break
                await asyncio.sleep(0.8 * attempt)
                continue

            raw = resp.content or b""
            if not raw:
                return {
                    "hour": hour,
                    "ticks": [],
                    "status": resp.status_code,
                    "classification": "EXPECTED_EMPTY",
                    "error": "empty body",
                    "attempts": attempt,
                    "url": url,
                }

            try:
                decoded = decompress(raw)
                ticks = parse_ticks(decoded, h0, divisor)
            except Exception as e:  # noqa: BLE001
                last_error = f"decode: {e}"
                if attempt >= _MAX_OTHER_ATTEMPTS:
                    break
                await asyncio.sleep(0.5 * attempt)
                continue

            return {
                "hour": hour,
                "ticks": ticks,
                "status": resp.status_code,
                "classification": "SUCCESS" if ticks else "EXPECTED_EMPTY",
                "error": None if ticks else "no ticks after parse",
                "attempts": attempt,
                "url": url,
            }
        except Exception as e:  # noqa: BLE001
            last_error = str(e)
            if attempt >= _MAX_OTHER_ATTEMPTS:
                break
            await asyncio.sleep(1.0 + attempt)

    return {
        "hour": hour,
        "ticks": [],
        "status": last_status,
        "classification": "FAILED",
        "error": last_error or "unknown",
        "attempts": attempt,
        "url": url,
    }


def classify_day(hour_results: list[dict[str, Any]]) -> str:
    """Day status from hour classifications. Any FAILED → FAILED."""
    if not hour_results:
        return "MISSING"
    classes = [h["classification"] for h in hour_results]
    if any(c == "FAILED" for c in classes):
        return "FAILED"
    if all(c == "EXPECTED_EMPTY" for c in classes):
        return "EXPECTED_EMPTY"
    if any(c == "SUCCESS" for c in classes):
        return "SUCCESS"
    return "MISSING"


async def download_day(
    symbol: str,
    day: date,
    *,
    client: httpx.AsyncClient | None = None,
    hours: list[int] | None = None,
    seed_hour_results: list[dict[str, Any]] | None = None,
    on_hour: Any | None = None,
) -> dict[str, Any]:
    """Download selected hours (default 0..23).

    seed_hour_results: prior SUCCESS/EXPECTED_EMPTY hour dicts to merge for day classification
    without re-fetching.
    """
    own = client is None
    if own:
        client = httpx.AsyncClient(headers={"User-Agent": "tr-market-data/0.1"})
    assert client is not None
    target_hours = list(range(24) if hours is None else hours)
    hour_results: list[dict[str, Any]] = list(seed_hour_results or [])
    try:
        for hour in target_hours:
            hr = await download_hour_ticks(symbol, day, hour, client)
            hour_results.append(hr)
            if on_hour is not None:
                try:
                    on_hour(hr)
                except Exception:
                    pass
    finally:
        if own:
            await client.aclose()

    # Dedupe by hour (last write wins)
    by_hour: dict[int, dict[str, Any]] = {}
    for h in hour_results:
        by_hour[int(h["hour"])] = h
    hours_list = [by_hour[h] for h in sorted(by_hour)]

    all_ticks: list[dict[str, Any]] = []
    for h in hours_list:
        all_ticks.extend(h.get("ticks") or [])
    all_ticks.sort(key=lambda t: t["time"])

    ok = sum(1 for h in hours_list if h["classification"] == "SUCCESS")
    empty = sum(1 for h in hours_list if h["classification"] == "EXPECTED_EMPTY")
    failed = sum(1 for h in hours_list if h["classification"] == "FAILED")
    # Only classify over hours we know about; missing hours → incomplete → FAILED if expected 24
    if hours is not None and len(by_hour) < 24:
        # incomplete day until all 24 classified
        if failed:
            day_status = "FAILED"
        elif ok or empty:
            day_status = "INCOMPLETE"
        else:
            day_status = "MISSING"
    else:
        day_status = classify_day(hours_list)

    return {
        "symbol": normalize_symbol(symbol),
        "request_symbol": symbol,
        "day": day.isoformat(),
        "status": day_status,
        "hours": hours_list,
        "ticks": all_ticks,
        "tick_count": len(all_ticks),
        "successful_hours": ok,
        "empty_hours": empty,
        "failed_hours": failed,
    }
