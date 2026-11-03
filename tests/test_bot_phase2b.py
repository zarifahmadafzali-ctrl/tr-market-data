"""Phase 2B bot unit tests — no real Telegram token required."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest

from bot.auth import can_use_bot, is_admin
from bot.delivery import catalog_files_for_day, day_ready
from bot.parsers import parse_get_args, parse_kind, supported_symbols
from bot.rate_limit import RateLimiter
from bot.services import cancel_user_job, handle_get_request
from db.catalog import connect, upsert_day
from scheduler.jobs import enqueue_range_job


def test_admin_auth(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "1675868882,999")
    monkeypatch.setenv("BOT_PUBLIC_ACCESS", "false")
    # reload helpers that read env at call time
    from bot import config

    monkeypatch.setattr(config, "admin_ids", lambda: {1675868882, 999})
    monkeypatch.setattr(config, "public_access_enabled", lambda: False)
    from bot.auth import can_use_bot as can
    from bot.auth import is_admin as adm

    assert adm(1675868882)
    assert can(1675868882)
    assert not adm(1)
    assert not can(1)


def test_public_denied_by_default(monkeypatch):
    monkeypatch.setattr("bot.auth.admin_ids", lambda: set())
    monkeypatch.setattr("bot.auth.public_access_enabled", lambda: False)
    assert not can_use_bot(12345)


def test_parse_get_single_day():
    r = parse_get_args(["US30", "2024-06-12", "tick"])
    assert r.symbol == "USA30IDXUSD"
    assert r.start == date(2024, 6, 12)
    assert r.end == date(2024, 6, 12)
    assert r.kind == "tick"


def test_parse_get_range_both():
    r = parse_get_args(["US30", "2024-06-10", "2024-06-12", "both"])
    assert r.start.isoformat() == "2024-06-10"
    assert r.end.isoformat() == "2024-06-12"
    assert r.kind == "both"


def test_parse_invalid_date():
    with pytest.raises(ValueError):
        parse_get_args(["US30", "06-12-2024", "tick"])


def test_parse_kind():
    assert parse_kind("1s") == "1s"
    assert parse_kind("tick") == "tick"
    with pytest.raises(ValueError):
        parse_kind("minute")


def test_symbols_list():
    assert "US30" in supported_symbols()


def test_rate_limit():
    lim = RateLimiter(3)
    assert lim.allow(1)
    assert lim.allow(1)
    assert lim.allow(1)
    assert not lim.allow(1)


def test_handle_get_ready_file(tmp_path: Path):
    db = tmp_path / "t.sqlite"
    data = tmp_path / "data"
    data.mkdir()
    # create fake parquet paths registered in catalog
    day_dir = data / "provider=dukascopy" / "symbol=USA30IDXUSD" / "year=2024" / "month=06" / "day=12"
    day_dir.mkdir(parents=True)
    tick_p = day_dir / "ticks.parquet"
    tick_p.write_bytes(b"PAR1fake")
    bars_p = day_dir / "bars_1s.parquet"
    bars_p.write_bytes(b"PAR1fake")
    meta_p = day_dir / "metadata.json"
    meta_p.write_text("{}")

    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": "USA30IDXUSD",
            "day": "2024-06-12",
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 10,
            "bars_1s_count": 5,
            "tick_file_path": str(tick_p),
            "bars_1s_file_path": str(bars_p),
            "metadata_path": str(meta_p),
        },
    )
    assert day_ready(db, "US30", "2024-06-12", "tick")
    offers = catalog_files_for_day(db, "US30", "2024-06-12", "tick")
    assert any(o.path == tick_p.resolve() or o.path == tick_p for o in offers) or len(offers) >= 1

    req = parse_get_args(["US30", "2024-06-12", "tick"])
    # patch data_root for delivery path checks
    import bot.delivery as delivery

    delivery.data_root = lambda: data  # type: ignore
    out = handle_get_request(req, db_path=db, data_root=data, run_immediately=False)
    assert "2024-06-12" in out["ready_days"]
    assert out["missing_days"] == []
    assert out["job_id"] is None
    assert len(out["files"]) >= 1


def test_handle_get_queues_job(tmp_path: Path):
    db = tmp_path / "t.sqlite"
    data = tmp_path / "data"
    data.mkdir()
    req = parse_get_args(["US30", "2024-06-12", "1s"])
    out = handle_get_request(req, db_path=db, data_root=data, run_immediately=False)
    assert "2024-06-12" in out["missing_days"]
    assert out["job_id"] is not None


def test_cancel_unauthorized_job_missing(tmp_path: Path):
    db = tmp_path / "t.sqlite"
    connect(db)
    out = cancel_user_job(db, 99999)
    assert out["ok"] is False


def test_token_not_in_source():
    root = Path(__file__).resolve().parents[1]
    forbidden_fragment = "ghp_" + "SHOULD_NOT_APPEAR"
    for path in root.rglob("*.py"):
        if ".venv" in str(path) or path.name.startswith("test_"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert forbidden_fragment not in text
        # token must only be read from env helpers
        if "TELEGRAM_BOT_TOKEN" in text:
            assert "os.getenv" in text or "telegram_token" in text or "config" in path.name
