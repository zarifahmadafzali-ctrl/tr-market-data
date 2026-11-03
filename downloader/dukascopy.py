"""
Dukascopy historical tick downloader (pilot).

Notes:
- Dukascopy historical URL months are zero-based (January = 00).
- Be polite: sequential hours, retries, backoff on HTTP 429.
- This module only downloads raw hourly .bi5 payloads; decoding is separate.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# Common index CFD id used by Dukascopy for Dow-like feeds.
DEFAULT_SYMBOLS = {
    "US30": "US30IDXUSD",
}


@dataclass
class HourJob:
    symbol: str
    dukascopy_id: str
    day: date
    hour: int


def dukascopy_hour_url(dukascopy_id: str, day: date, hour: int) -> str:
    # Month is zero-based in Dukascopy paths.
    return (
        f"https://datafeed.dukascopy.com/datafeed/"
        f"{dukascopy_id}/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
        f"{hour:02d}h_ticks.bi5"
    )


async def download_hour(
    client: httpx.AsyncClient,
    job: HourJob,
    dest: Path,
    *,
    min_interval_sec: float = 0.35,
    max_retries: int = 5,
) -> tuple[str, int]:
    """
    Returns (status, nbytes)
    status: success | expected_empty | failed
    """
    url = dukascopy_hour_url(job.dukascopy_id, job.day, job.hour)
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = ""
    for attempt in range(max_retries):
        try:
            await asyncio.sleep(min_interval_sec)
            r = await client.get(url, timeout=60.0)
            if r.status_code in (404, 204):
                return "expected_empty", 0
            if r.status_code == 429:
                wait = min(60.0, (2**attempt) * 2.0)
                ra = r.headers.get("Retry-After")
                if ra and ra.isdigit():
                    wait = max(wait, float(ra))
                log.warning("429 for %s — sleep %.1fs", url, wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code >= 400:
                last_err = f"HTTP {r.status_code}"
                await asyncio.sleep(1.0 + attempt)
                continue
            data = r.content
            if not data:
                return "expected_empty", 0
            dest.write_bytes(data)
            return "success", len(data)
        except Exception as e:  # noqa: BLE001 — pilot network path
            last_err = str(e)
            await asyncio.sleep(1.0 + attempt)
    log.error("hour failed %s: %s", url, last_err)
    return "failed", 0


async def download_day_hours(
    symbol: str,
    day: date,
    out_dir: Path,
    *,
    dukascopy_id: str | None = None,
    min_interval_sec: float = 0.35,
) -> dict:
    dc_id = dukascopy_id or DEFAULT_SYMBOLS.get(symbol.upper())
    if not dc_id:
        raise ValueError(f"Unknown symbol mapping: {symbol}")
    results = {"symbol": symbol, "day": day.isoformat(), "hours": {}, "bytes": 0, "failed": 0, "ok": 0, "empty": 0}
    async with httpx.AsyncClient(headers={"User-Agent": "tr-market-data/0.1"}) as client:
        for hour in range(24):
            job = HourJob(symbol=symbol, dukascopy_id=dc_id, day=day, hour=hour)
            dest = out_dir / symbol.upper() / day.isoformat() / f"{hour:02d}h_ticks.bi5"
            status, nbytes = await download_hour(client, job, dest, min_interval_sec=min_interval_sec)
            results["hours"][f"{hour:02d}"] = status
            results["bytes"] += nbytes
            if status == "success":
                results["ok"] += 1
            elif status == "expected_empty":
                results["empty"] += 1
            else:
                results["failed"] += 1
    return results
