from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader.dukascopy import classify_day, download_hour_ticks, hour_url


def test_zero_based_month_june():
    url = hour_url("USA30IDXUSD", date(2024, 6, 12), 0)
    assert "/2024/05/12/00h_ticks.bi5" in url


@pytest.mark.asyncio
async def test_404_expected_empty():
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 404
    resp.headers = {}
    resp.content = b""
    client.get = AsyncMock(return_value=resp)
    r = await download_hour_ticks("US30", date(2024, 6, 12), 0, client)
    assert r["classification"] == "EXPECTED_EMPTY"


@pytest.mark.asyncio
async def test_429_eventually_failed(monkeypatch):
    import downloader.dukascopy as dk

    monkeypatch.setattr(dk, "_429_BACKOFF", (0.01, 0.01, 0.01, 0.01))
    client = AsyncMock()
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    resp.content = b""
    client.get = AsyncMock(return_value=resp)
    r = await download_hour_ticks("US30", date(2024, 6, 12), 3, client)
    assert r["classification"] == "FAILED"
    assert r["status"] == 429
    assert client.get.call_count >= 4


def test_classify_day_failed_wins():
    hours = [
        {"classification": "SUCCESS"},
        {"classification": "FAILED"},
        {"classification": "EXPECTED_EMPTY"},
    ]
    assert classify_day(hours) == "FAILED"


def test_classify_all_empty():
    hours = [{"classification": "EXPECTED_EMPTY"}] * 24
    assert classify_day(hours) == "EXPECTED_EMPTY"
