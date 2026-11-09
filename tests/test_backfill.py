"""Tests for legacy Telegram archive backfill into GitHub manifest + SQLite."""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.archive import (
    ARTIFACT_BARS_1S,
    ARTIFACT_META,
    ARTIFACT_TICKS,
    STATUS_INDEXED,
    get_indexed_artifacts,
    package_fully_indexed,
)
from bot.backfill import (
    BackfillError,
    parse_archive_document,
    register_legacy_artifact,
)
from bot.manifest import empty_manifest, merge_artifact_into_manifest
from bot.parsers import GetRequest
from bot.services import handle_get_request
from db.catalog import connect


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "bf.sqlite"
    connect(path)
    return path


def test_parse_artifact_key_caption():
    p = parse_archive_document(
        file_id="AgAD_file_1",
        file_name="ticks.parquet",
        caption="dukascopy|USA30IDXUSD|2026-11-07|ticks_parquet\nsha256=abc123def456",
        file_size=1000,
    )
    assert p.symbol == "USA30IDXUSD"
    assert p.day == "2026-11-07"
    assert p.artifact_type == ARTIFACT_TICKS
    assert p.sha256.startswith("abc123")
    assert p.file_id == "AgAD_file_1"


def test_parse_human_caption_with_hint():
    p = parse_archive_document(
        file_id="fid2",
        file_name="bars_1s.parquet",
        caption="some random text",
        hint_symbol="US30",
        hint_day="2026-11-07",
    )
    assert p.symbol == "USA30IDXUSD"
    assert p.day == "2026-11-07"
    assert p.artifact_type == ARTIFACT_BARS_1S


def test_parse_filename_only_needs_hints():
    with pytest.raises(BackfillError):
        parse_archive_document(file_id="x", file_name="ticks.parquet")

    p = parse_archive_document(
        file_id="x",
        file_name="metadata.json",
        hint_symbol="EURUSD",
        hint_day="2026-01-15",
    )
    assert p.artifact_type == ARTIFACT_META
    assert p.symbol == "EURUSD"


def test_register_three_artifacts_no_download(db: Path, monkeypatch):
    # Disable remote manifest for unit test; still marks SQLite INDEXED
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "false")

    docs = [
        ("ticks.parquet", ARTIFACT_TICKS, "fid_tick"),
        ("bars_1s.parquet", ARTIFACT_BARS_1S, "fid_bars"),
        ("metadata.json", ARTIFACT_META, "fid_meta"),
    ]
    for fname, atype, fid in docs:
        parsed = parse_archive_document(
            file_id=fid,
            file_name=fname,
            caption=f"dukascopy|USA30IDXUSD|2026-11-07|{atype}",
        )
        result = register_legacy_artifact(db, parsed, write_manifest=False)
        assert result["ok"] is True
        assert result["action"] in ("inserted", "already_indexed", "updated")

    assert package_fully_indexed(db, "US30", "2026-11-07", "both")
    arts = get_indexed_artifacts(db, "US30", "2026-11-07", "both")
    assert len(arts) >= 2  # ticks + bars (meta may be extra)
    fids = {a["telegram_file_id"] for a in arts}
    assert "fid_tick" in fids
    assert "fid_bars" in fids


def test_duplicate_backfill_idempotent(db: Path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "false")
    parsed = parse_archive_document(
        file_id="same_fid",
        file_name="ticks.parquet",
        caption="dukascopy|USA30IDXUSD|2026-11-07|ticks_parquet",
    )
    r1 = register_legacy_artifact(db, parsed, write_manifest=False)
    r2 = register_legacy_artifact(db, parsed, write_manifest=False)
    assert r1["ok"] and r2["ok"]
    assert r2["action"] == "already_indexed"


def test_conflict_different_file_id(db: Path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "false")
    p1 = parse_archive_document(
        file_id="fid_a",
        file_name="ticks.parquet",
        caption="dukascopy|USA30IDXUSD|2026-11-07|ticks_parquet",
    )
    register_legacy_artifact(db, p1, write_manifest=False)
    p2 = parse_archive_document(
        file_id="fid_b",
        file_name="ticks.parquet",
        caption="dukascopy|USA30IDXUSD|2026-11-07|ticks_parquet",
    )
    r = register_legacy_artifact(db, p2, write_manifest=False)
    assert r["ok"] is False
    assert r["action"] == "conflict"


def test_get_uses_backfilled_index_no_job(db: Path, monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-100123")
    # Ensure archive_enabled
    for fname, atype, fid in [
        ("ticks.parquet", "ticks_parquet", "T1"),
        ("bars_1s.parquet", "bars_1s_parquet", "B1"),
    ]:
        p = parse_archive_document(
            file_id=fid,
            file_name=fname,
            caption=f"dukascopy|USA30IDXUSD|2026-11-07|{atype}",
        )
        register_legacy_artifact(db, p, write_manifest=False)

    req = GetRequest(
        symbol="US30",
        start=__import__("datetime").date(2026, 11, 7),
        end=__import__("datetime").date(2026, 11, 7),
        kind="tick",
    )
    result = handle_get_request(req, db_path=db, data_root=tmp_path)
    assert "2026-11-07" in result["ready_days"]
    assert result["job_id"] is None
    assert any(f.get("file_id") == "T1" for f in result["files"])


def test_invalid_incomplete_metadata():
    with pytest.raises(BackfillError):
        parse_archive_document(file_id="x", caption="not useful")
