"""Telegram channel archive: keys, checksums, state machine, recovery, /get order."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from bot.archive import (
    ARTIFACT_BARS_1S,
    ARTIFACT_META,
    ARTIFACT_TICKS,
    STATUS_FAILED,
    STATUS_INDEXED,
    STATUS_PENDING,
    STATUS_UPLOADING,
    artifact_key,
    backoff_seconds,
    enqueue_archive_for_day,
    get_indexed_artifacts,
    package_fully_indexed,
    package_key,
    process_one_artifact,
    recover_stale_uploading,
    sha256_file,
)
from bot.parsers import GetRequest
from bot.services import handle_get_request
from db.catalog import connect, upsert_day
from downloader.symbols import normalize_symbol


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "t.sqlite"
    connect(path)  # applies schema
    return path


def _write_day_files(root: Path, symbol: str, day: str, provider: str = "dukascopy"):
    d = date.fromisoformat(day)
    day_path = (
        root
        / f"provider={provider}"
        / f"symbol={symbol}"
        / f"year={d.year:04d}"
        / f"month={d.month:02d}"
        / f"day={d.day:02d}"
    )
    day_path.mkdir(parents=True)
    files = {
        "ticks.parquet": b"TICKDATA" + b"\x00" * 100,
        "bars_1s.parquet": b"BARSDATA" + b"\x01" * 80,
        "metadata.json": b'{"ok":true}',
    }
    paths = {}
    for name, content in files.items():
        p = day_path / name
        p.write_bytes(content)
        paths[name] = p
    return paths


def _seed_catalog(db: Path, symbol: str, day: str, paths: dict, provider: str = "dukascopy"):
    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": symbol,
            "provider": provider,
            "day": day,
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 10,
            "bars_1s_count": 5,
            "successful_hours": 24,
            "empty_hours": 0,
            "failed_hours": 0,
            "tick_file_path": str(paths["ticks.parquet"]),
            "bars_1s_file_path": str(paths["bars_1s.parquet"]),
            "metadata_path": str(paths["metadata.json"]),
            "error": None,
        },
    )


def test_package_and_artifact_keys_stable():
    # This repo canonicalizes US30 → USA30IDXUSD (Dukascopy id)
    assert package_key("dukascopy", "us30", "2024-06-12") == "dukascopy|USA30IDXUSD|2024-06-12"
    assert package_key("dukascopy", "USA30IDXUSD", "2024-06-12") == "dukascopy|USA30IDXUSD|2024-06-12"
    assert artifact_key("dukascopy", "US30", "2024-06-12", ARTIFACT_TICKS).endswith(
        "|ticks_parquet"
    )


def test_sha256_file(tmp_path: Path):
    p = tmp_path / "a.bin"
    data = b"hello-archive"
    p.write_bytes(data)
    assert sha256_file(p) == hashlib.sha256(data).hexdigest()


def test_backoff_bounds():
    assert backoff_seconds(0, base=30, cap=3600) == 30
    assert backoff_seconds(1, base=30, cap=3600) == 60
    assert backoff_seconds(20, base=30, cap=3600) == 3600


def test_enqueue_and_unique(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    r1 = enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)
    r2 = enqueue_archive_for_day(db, "USA30IDXUSD", "2024-06-12", root=root)
    assert r1["job_id"] == r2["job_id"]
    conn = connect(db)
    n = conn.execute("SELECT COUNT(*) AS c FROM archived_artifacts").fetchone()["c"]
    assert n == 3
    # unique constraint on artifact_key
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO archived_artifacts (archive_job_id, artifact_key, symbol, provider, day, "
            "artifact_type, file_name, sha256, status, created_at, updated_at) "
            "VALUES (1, 'dukascopy|USA30IDXUSD|2024-06-12|ticks_parquet', 'USA30IDXUSD', 'dukascopy', "
            "'2024-06-12', 'ticks_parquet', 'ticks.parquet', 'abc', 'PENDING', 't', 't')"
        )
        conn.commit()


def test_upload_index_and_no_reupload(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-100123")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)

    calls = []

    def fake_upload(path: Path, caption: str, chat_id: int, token: str):
        calls.append(path.name)
        return {
            "file_id": f"fid-{path.name}",
            "file_unique_id": f"uniq-{path.name}",
            "message_id": 100 + len(calls),
            "chat_id": chat_id,
        }

    for _ in range(5):
        process_one_artifact(
            db, upload_fn=fake_upload, token="t", channel_id=-100123
        )
    assert len(calls) == 3
    # further ticks no-op (nothing pending)
    assert process_one_artifact(db, upload_fn=fake_upload, token="t", channel_id=-100123) is None
    assert package_fully_indexed(db, "US30", "2024-06-12", "both")
    indexed = get_indexed_artifacts(db, "US30", "2024-06-12", "both")
    assert len(indexed) >= 2
    assert all(r["telegram_file_id"] for r in indexed)


def test_size_limit_permanent_fail(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-100123")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    # make ticks huge in catalog size by writing big file
    paths["ticks.parquet"].write_bytes(b"x" * (46 * 1024 * 1024))
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)

    def boom(*a, **k):
        raise AssertionError("should not upload oversized")

    # process until ticks claimed
    results = []
    for _ in range(5):
        r = process_one_artifact(db, upload_fn=boom, token="t", channel_id=-100123)
        if r:
            results.append(r)
    assert any(r.get("status") == STATUS_FAILED and "exceeds" in (r.get("error") or "") for r in results)


def test_stale_uploading_recovery(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-1001")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)
    conn = connect(db)
    row = conn.execute("SELECT id FROM archived_artifacts LIMIT 1").fetchone()
    aid = int(row["id"])
    conn.execute(
        "UPDATE archived_artifacts SET status=? WHERE id=?",
        (STATUS_UPLOADING, aid),
    )
    conn.commit()
    n = recover_stale_uploading(db)
    assert n >= 1
    st = conn.execute("SELECT status FROM archived_artifacts WHERE id=?", (aid,)).fetchone()["status"]
    assert st == STATUS_PENDING


def test_response_ok_recovery_indexes(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-1001")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)
    conn = connect(db)
    row = conn.execute("SELECT id FROM archived_artifacts LIMIT 1").fetchone()
    aid = int(row["id"])
    conn.execute(
        "UPDATE archived_artifacts SET status=? WHERE id=?",
        (STATUS_UPLOADING, aid),
    )
    conn.execute(
        "INSERT INTO archive_upload_attempts (artifact_id, attempt_no, status, local_sha256, "
        "telegram_file_id, telegram_message_id, started_at, finished_at) "
        "VALUES (?, 1, 'RESPONSE_OK', 'abc', 'fid-recovered', 42, 't', 't')",
        (aid,),
    )
    conn.commit()
    recover_stale_uploading(db)
    st = conn.execute(
        "SELECT status, telegram_file_id FROM archived_artifacts WHERE id=?", (aid,)
    ).fetchone()
    assert st["status"] == STATUS_INDEXED
    assert st["telegram_file_id"] == "fid-recovered"


def test_get_archive_hit_no_download(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-1001")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)

    def fake_upload(path: Path, caption: str, chat_id: int, token: str):
        return {
            "file_id": f"fid-{path.name}",
            "file_unique_id": "u",
            "message_id": 1,
            "chat_id": chat_id,
        }

    for _ in range(5):
        process_one_artifact(db, upload_fn=fake_upload, token="t", channel_id=-1001)

    req = GetRequest(symbol="US30", start=date(2024, 6, 12), end=date(2024, 6, 12), kind="both")
    result = handle_get_request(req, db_path=db, data_root=root, run_immediately=False)
    assert result["missing_days"] == []
    assert result["job_id"] is None
    assert any(f.get("source") == "telegram_archive" for f in result["files"])


def test_get_partial_archive_no_redownload(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    # day ready locally, archive not fully indexed
    req = GetRequest(symbol="US30", start=date(2024, 6, 12), end=date(2024, 6, 12), kind="tick")
    result = handle_get_request(req, db_path=db, data_root=root, run_immediately=False)
    assert result["missing_days"] == []
    assert result["job_id"] is None
    assert result["ready_days"] == ["2024-06-12"]


def test_get_missing_enqueues_download(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "false")
    root = tmp_path / "data"
    root.mkdir()
    req = GetRequest(symbol="US30", start=date(2024, 6, 12), end=date(2024, 6, 12), kind="tick")
    result = handle_get_request(req, db_path=db, data_root=root, run_immediately=False)
    assert result["missing_days"] == ["2024-06-12"]
    assert result["job_id"] is not None


def test_symbol_alias_us30(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    r = enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)
    assert r["job_id"]
    assert normalize_symbol("US30") == "USA30IDXUSD"
    assert package_key("dukascopy", "US30", "2024-06-12") == package_key(
        "dukascopy", "USA30IDXUSD", "2024-06-12"
    )


def test_no_cleanup_before_indexed(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("ALLOW_DELETE_STAGING", "false")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-1001")
    root = tmp_path / "data"
    paths = _write_day_files(root, "USA30IDXUSD", "2024-06-12")
    _seed_catalog(db, "USA30IDXUSD", "2024-06-12", paths)
    enqueue_archive_for_day(db, "US30", "2024-06-12", root=root)

    def fake_upload(path: Path, caption: str, chat_id: int, token: str):
        return {"file_id": "fid", "file_unique_id": "u", "message_id": 1, "chat_id": chat_id}

    process_one_artifact(db, upload_fn=fake_upload, token="t", channel_id=-1001)
    assert paths["ticks.parquet"].is_file() or paths["bars_1s.parquet"].is_file()
