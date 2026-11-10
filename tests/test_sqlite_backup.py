"""SQLite GitHub mirror: snapshot + API mocks."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

import pytest

from bot.sqlite_backup import consistent_snapshot, fetch_remote_sqlite, put_remote_sqlite
from db.catalog import connect


def test_consistent_snapshot(tmp_path: Path):
    db = tmp_path / "a.sqlite"
    conn = connect(db)
    conn.execute(
        "INSERT INTO download_jobs (kind, symbols_json, start_day, end_day, provider, status, progress_json, retry_count, max_retries, created_at, updated_at) VALUES (?,?,?,?,?,?,?,0,3,?,?)",
        ("range", '["US30"]', "2024-01-01", "2024-01-01", "dukascopy", "QUEUED", "{}", "t", "t"),
    )
    conn.commit()
    conn.close()
    dest = tmp_path / "snap.sqlite"
    n = consistent_snapshot(db, dest)
    assert n > 0
    c2 = sqlite3.connect(str(dest))
    row = c2.execute("SELECT status FROM download_jobs").fetchone()
    assert row[0] == "QUEUED"
    c2.close()


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.puts = 0

    def get(self, url, headers=None, params=None):
        if self.mode == "missing":
            return _FakeResp(404)
        if self.mode == "ok_get":
            raw = b"SQLite format 3\x00fake"
            return _FakeResp(
                200,
                {
                    "sha": "abc",
                    "encoding": "base64",
                    "content": base64.b64encode(raw).decode(),
                },
            )
        return _FakeResp(404)

    def put(self, url, headers=None, json=None):
        self.puts += 1
        return _FakeResp(201, {"content": {"sha": "newsha"}})

    def close(self):
        pass


def test_fetch_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SQLITE_BACKUP_ENABLED", "true")
    dest = tmp_path / "db.sqlite"
    r = fetch_remote_sqlite(
        dest=dest, token="t", repo="o/r", path="backups/x.sqlite", client=_FakeClient("missing")
    )
    assert r["status"] == "missing"


def test_fetch_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SQLITE_BACKUP_ENABLED", "true")
    dest = tmp_path / "db.sqlite"
    r = fetch_remote_sqlite(
        dest=dest, token="t", repo="o/r", path="backups/x.sqlite", client=_FakeClient("ok_get")
    )
    assert r["status"] == "ok"
    assert dest.is_file()


def test_put_ok(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SQLITE_BACKUP_ENABLED", "true")
    f = tmp_path / "snap.sqlite"
    f.write_bytes(b"SQLite format 3\x00data")
    client = _FakeClient()
    r = put_remote_sqlite(f, token="t", repo="o/r", path="backups/x.sqlite", client=client)
    assert r["status"] == "ok"
    assert client.puts == 1
