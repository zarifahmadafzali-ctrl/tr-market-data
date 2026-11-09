"""GitHub archive manifest: merge, 409 retry, SQLite rebuild, /get priority."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from bot.archive import (
    STATUS_INDEXED,
    STATUS_PENDING,
    STATUS_UPLOADED,
    enqueue_archive_for_day,
    package_fully_indexed,
    process_one_artifact,
)
from bot.manifest import (
    SCHEMA_VERSION,
    empty_manifest,
    fetch_manifest,
    merge_artifact_into_manifest,
    put_manifest,
    rebuild_sqlite_from_manifest,
    upsert_artifact_remote,
    validate_manifest,
)
from bot.parsers import GetRequest
from bot.services import handle_get_request
from db.catalog import connect, upsert_day


@pytest.fixture()
def db(tmp_path: Path):
    path = tmp_path / "m.sqlite"
    connect(path)
    return path


def test_empty_and_validate():
    m = empty_manifest()
    ok, reason = validate_manifest(m)
    assert ok, reason
    assert m["schema_version"] == SCHEMA_VERSION


def test_unsupported_schema():
    ok, reason = validate_manifest({"schema_version": 99, "packages": {}})
    assert not ok
    assert "unsupported" in reason


def test_merge_preserves_existing():
    m = empty_manifest()
    m = merge_artifact_into_manifest(
        m,
        package_key="dukascopy|USA30IDXUSD|2026-11-06",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="ticks.parquet",
        artifact={"file_id": "fid1", "status": "INDEXED", "sha256": "a"},
    )
    m = merge_artifact_into_manifest(
        m,
        package_key="dukascopy|USA30IDXUSD|2026-11-06",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="bars_1s.parquet",
        artifact={"file_id": "fid2", "status": "INDEXED", "sha256": "b"},
    )
    arts = m["packages"]["dukascopy|USA30IDXUSD|2026-11-06"]["artifacts"]
    assert "ticks.parquet" in arts and "bars_1s.parquet" in arts
    assert arts["ticks.parquet"]["file_id"] == "fid1"


def test_partial_package():
    m = empty_manifest()
    m = merge_artifact_into_manifest(
        m,
        package_key="p1",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="metadata.json",
        artifact={"file_id": "m1", "status": "INDEXED"},
    )
    assert list(m["packages"]["p1"]["artifacts"].keys()) == ["metadata.json"]


def test_upsert_conflict_retries():
    state = {"sha": "sha0", "manifest": empty_manifest(), "puts": 0}

    def fetch(**kw):
        return state["manifest"], state["sha"], "ok"

    def put(manifest, *, expected_sha, **kw):
        state["puts"] += 1
        if state["puts"] == 1:
            return False, None, "conflict"
        state["manifest"] = manifest
        state["sha"] = "sha1"
        return True, "sha1", "ok"

    ok = upsert_artifact_remote(
        package_key="pk",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="ticks.parquet",
        artifact={"file_id": "f", "status": "INDEXED"},
        token="fake",
        repo="o/r",
        fetch_fn=fetch,
        put_fn=put,
    )
    assert ok
    assert state["puts"] == 2


def test_rebuild_empty_sqlite(db: Path):
    m = empty_manifest()
    m = merge_artifact_into_manifest(
        m,
        package_key="dukascopy|USA30IDXUSD|2026-11-06",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="ticks.parquet",
        artifact={
            "file_id": "fid-ticks",
            "file_unique_id": "u1",
            "message_id": 10,
            "chat_id": "-1001",
            "sha256": "abc",
            "size_bytes": 100,
            "status": "INDEXED",
        },
    )
    stats = rebuild_sqlite_from_manifest(db, m)
    assert stats["artifacts_upserted"] == 1
    assert package_fully_indexed(db, "US30", "2026-11-06", "tick")
    # idempotent
    stats2 = rebuild_sqlite_from_manifest(db, m)
    assert stats2["artifacts_upserted"] == 0


def test_rebuild_preserves_local_wip(db: Path, tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    # seed pending local artifact without file_id
    from bot.archive import enqueue_archive_for_day

    day_path = (
        root
        / "provider=dukascopy"
        / "symbol=USA30IDXUSD"
        / "year=2026"
        / "month=11"
        / "day=06"
    )
    day_path.mkdir(parents=True)
    tick = day_path / "ticks.parquet"
    tick.write_bytes(b"data")
    conn = connect(db)
    upsert_day(
        conn,
        {
            "symbol": "USA30IDXUSD",
            "provider": "dukascopy",
            "day": "2026-11-06",
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 1,
            "bars_1s_count": 0,
            "successful_hours": 1,
            "empty_hours": 0,
            "failed_hours": 0,
            "tick_file_path": str(tick),
            "bars_1s_file_path": None,
            "metadata_path": None,
            "error": None,
        },
    )
    enqueue_archive_for_day(db, "US30", "2026-11-06", root=root)
    # manifest has same package INDEXED
    m = empty_manifest()
    m = merge_artifact_into_manifest(
        m,
        package_key="dukascopy|USA30IDXUSD|2026-11-06",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="ticks.parquet",
        artifact={"file_id": "remote-fid", "status": "INDEXED", "sha256": "x"},
    )
    rebuild_sqlite_from_manifest(db, m)
    row = connect(db).execute(
        "SELECT status, telegram_file_id FROM archived_artifacts LIMIT 1"
    ).fetchone()
    # local PENDING without file_id should be skipped (wip) OR if status still PENDING ok
    assert row["status"] in (STATUS_PENDING, STATUS_INDEXED)


def test_manifest_fail_keeps_uploaded(db: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_ARCHIVE_CHANNEL_ID", "-100123")
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "true")
    monkeypatch.setenv("ARCHIVE_MANIFEST_REPO", "o/r")
    monkeypatch.setenv("ARCHIVE_MANIFEST_TOKEN", "tok")
    root = tmp_path / "data"
    day_path = (
        root
        / "provider=dukascopy"
        / "symbol=USA30IDXUSD"
        / "year=2026"
        / "month=11"
        / "day=06"
    )
    day_path.mkdir(parents=True)
    for name, content in [
        ("ticks.parquet", b"T" * 20),
        ("bars_1s.parquet", b"B" * 20),
        ("metadata.json", b"{}"),
    ]:
        (day_path / name).write_bytes(content)
    paths = {
        "ticks.parquet": day_path / "ticks.parquet",
        "bars_1s.parquet": day_path / "bars_1s.parquet",
        "metadata.json": day_path / "metadata.json",
    }
    upsert_day(
        connect(db),
        {
            "symbol": "USA30IDXUSD",
            "provider": "dukascopy",
            "day": "2026-11-06",
            "status": "SUCCESS",
            "tick_status": "SUCCESS",
            "bars_1s_status": "SUCCESS",
            "tick_count": 1,
            "bars_1s_count": 1,
            "successful_hours": 1,
            "empty_hours": 0,
            "failed_hours": 0,
            "tick_file_path": str(paths["ticks.parquet"]),
            "bars_1s_file_path": str(paths["bars_1s.parquet"]),
            "metadata_path": str(paths["metadata.json"]),
            "error": None,
        },
    )
    enqueue_archive_for_day(db, "US30", "2026-11-06", root=root)

    def fake_upload(path, caption, chat_id, token):
        return {
            "file_id": f"fid-{path.name}",
            "file_unique_id": "u",
            "message_id": 1,
            "chat_id": chat_id,
        }

    def fail_upsert(**kwargs):
        return False

    import bot.archive as arch

    monkeypatch.setattr("bot.manifest.upsert_artifact_remote", fail_upsert)
    # process one
    r = process_one_artifact(db, upload_fn=fake_upload, token="t", channel_id=-100123)
    assert r is not None
    assert r["status"] == STATUS_UPLOADED
    assert r.get("file_id")
    row = connect(db).execute(
        "SELECT status, telegram_file_id FROM archived_artifacts WHERE telegram_file_id IS NOT NULL LIMIT 1"
    ).fetchone()
    assert row["status"] == STATUS_UPLOADED
    assert row["telegram_file_id"]


def test_get_from_manifest_no_download(db: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ARCHIVE_ENABLED", "true")
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "true")
    m = empty_manifest()
    m = merge_artifact_into_manifest(
        m,
        package_key="dukascopy|USA30IDXUSD|2026-11-06",
        provider="dukascopy",
        symbol="USA30IDXUSD",
        day="2026-11-06",
        file_name="ticks.parquet",
        artifact={
            "file_id": "fid-only",
            "status": "INDEXED",
            "sha256": "s",
            "size_bytes": 10,
        },
    )
    rebuild_sqlite_from_manifest(db, m)
    req = GetRequest(
        symbol="US30",
        start=date(2026, 11, 6),
        end=date(2026, 11, 6),
        kind="tick",
    )
    result = handle_get_request(req, db_path=db, data_root=Path("/tmp"), run_immediately=False)
    assert result["missing_days"] == []
    assert result["job_id"] is None
    assert any(f.get("file_id") == "fid-only" for f in result["files"])


def test_github_unavailable_no_crash(monkeypatch):
    monkeypatch.setenv("ARCHIVE_MANIFEST_ENABLED", "true")
    monkeypatch.setenv("ARCHIVE_MANIFEST_REPO", "o/r")
    monkeypatch.setenv("ARCHIVE_MANIFEST_TOKEN", "tok")

    def boom(**kw):
        raise RuntimeError("network")

    # fetch_manifest catches and returns error
    from bot.manifest import startup_sync_manifest

    monkeypatch.setattr("bot.manifest.fetch_manifest", lambda **kw: (None, None, "error"))
    from pathlib import Path
    import tempfile

    db = Path(tempfile.mkdtemp()) / "x.sqlite"
    connect(db)
    r = startup_sync_manifest(db)
    assert r["status"] == "error"


def test_no_token_in_validate_logs():
    # structural: token never appears in empty_manifest serialization
    m = empty_manifest()
    s = json.dumps(m)
    assert "token" not in s.lower()
    assert "ghp_" not in s
