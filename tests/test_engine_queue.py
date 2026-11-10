"""Unit tests for engine queue + run loop (stdlib + fakes, no network)."""

from __future__ import annotations

import base64
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from engine.github_api import GitHubContents
from engine.notify import Notifier
from engine.queue_store import STATUS_DONE, STATUS_QUEUED, STATUS_RUNNING, QueueStore, new_request
from engine.run import process_request
from engine.config import EngineConfig


class MemTransport:
    """In-memory GitHub Contents fake."""

    def __init__(self) -> None:
        self.files: dict[str, dict[str, Any]] = {}

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        # url .../contents/<path>?ref=...
        path = url.split("/contents/")[-1].split("?")[0]
        if method == "GET":
            if path in self.files and not path.endswith("/") and path.count("/") >= 0:
                # file
                if path in self.files:
                    rec = self.files[path]
                    payload = {
                        "sha": rec["sha"],
                        "content": base64.b64encode(rec["raw"]).decode(),
                        "encoding": "base64",
                    }
                    return 200, json.dumps(payload).encode()
                return 404, b'{"message":"Not Found"}'
            # list dir
            prefix = path.rstrip("/")
            items = []
            for k, rec in self.files.items():
                if prefix and not k.startswith(prefix + "/") and k != prefix:
                    if prefix == "queue" and k.startswith("queue/"):
                        pass
                    else:
                        continue
                if prefix == "queue" and k.startswith("queue/") and k.count("/") == 1:
                    items.append({"type": "file", "name": k.split("/")[-1], "path": k})
                elif "/" not in k[len(prefix) + 1 :] if prefix else "/" not in k:
                    if k.startswith(prefix + "/") or (not prefix and "/" not in k):
                        items.append({"type": "file", "name": k.split("/")[-1], "path": k})
            # simpler list for queue/
            if "queue" in path or path.endswith("queue"):
                items = [
                    {"type": "file", "name": k.split("/")[-1], "path": k}
                    for k in self.files
                    if k.startswith("queue/") and k.endswith(".json")
                ]
                return 200, json.dumps(items).encode()
            return 404, b'[]'
        if method == "PUT":
            assert body is not None
            data = json.loads(body.decode())
            raw = base64.b64decode(data["content"])
            sha_in = data.get("sha")
            if path in self.files and self.files[path]["sha"] != sha_in:
                return 409, b'{"message":"Conflict"}'
            new_sha = "sha-" + str(len(raw)) + "-" + str(len(self.files) + 1)
            self.files[path] = {"sha": new_sha, "raw": raw}
            return 201, json.dumps({"content": {"sha": new_sha}}).encode()
        if method == "DELETE":
            self.files.pop(path, None)
            return 200, b"{}"
        return 500, b"{}"


class TestQueueStore(unittest.TestCase):
    def setUp(self) -> None:
        self.tr = MemTransport()
        self.gh = GitHubContents("owner/index", "tok", transport=self.tr)
        self.q = QueueStore(self.gh, prefix="queue")

    def test_create_and_next(self) -> None:
        req = new_request(symbol="US30", start="2024-06-10", end="2024-06-11")
        self.q.create(req)
        got = self.q.next_runnable(stale_sec=900)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["id"], req["id"])
        self.assertEqual(got["status"], STATUS_QUEUED)

    def test_stale_running_reclaimed(self) -> None:
        req = new_request(symbol="US30", start="2024-06-10", end="2024-06-10")
        self.q.create(req)
        self.q.update(
            req["id"],
            lambda r: {
                **r,
                "status": STATUS_RUNNING,
                "started_at": "2020-01-01T00:00:00+00:00",
                "heartbeat_at": "2020-01-01T00:00:00+00:00",
            },
        )
        got = self.q.next_runnable(stale_sec=900)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertEqual(got["id"], req["id"])


class TestProcessRequest(unittest.TestCase):
    def test_two_days_success(self) -> None:
        tr = MemTransport()
        gh = GitHubContents("owner/index", "tok", transport=tr)
        q = QueueStore(gh, prefix="queue")
        req = new_request(symbol="US30", start="2024-06-10", end="2024-06-11")
        q.create(req)
        notes: list[str] = []

        def fake_post(url: str, form: dict) -> tuple[int, dict]:
            notes.append(form.get("text", ""))
            return 200, {"ok": True}

        notifier = Notifier("bot-token", post=fake_post)
        cfg = EngineConfig(
            index_repo="owner/index",
            index_token="tok",
            index_branch="main",
            queue_prefix="queue",
            manifest_path="archive_manifest.json",
            telegram_token="bot-token",
            archive_channel_id=-1001,
            admin_ids=frozenset(),
            sqlite_path=Path("/tmp/x.sqlite"),
            data_root=Path("/tmp/data"),
            max_seconds=3600,
            day_margin_sec=60,
            stale_sec=900,
            max_archive_steps_per_day=3,
            workflow_repo="owner/code",
            workflow_id="engine.yml",
            github_token_for_dispatch="tok",
        )

        def day_runner(symbol, day, **kwargs):
            return {"status": "SUCCESS", "symbol": symbol, "day": day.isoformat()}

        def archive_day(symbol, day, **kwargs):
            return {"status": "INDEXED"}

        def is_indexed(symbol, day, **kwargs):
            return False

        out = process_request(
            req,
            cfg,
            queue=q,
            notifier=notifier,
            day_runner=day_runner,
            archive_day=archive_day,
            is_indexed=is_indexed,
            redispatch=lambda _rid: True,
        )
        self.assertEqual(out["status"], STATUS_DONE)
        self.assertEqual(len(out["progress"]["done_days"]), 2)
        final, _ = q.get(req["id"])
        assert final is not None
        self.assertEqual(final["status"], STATUS_DONE)

    def test_time_budget_pauses(self) -> None:
        tr = MemTransport()
        gh = GitHubContents("owner/index", "tok", transport=tr)
        q = QueueStore(gh, prefix="queue")
        req = new_request(symbol="US30", start="2024-06-10", end="2024-06-12")
        q.create(req)
        notifier = Notifier("bot-token", post=lambda u, f: (200, {"ok": True}))
        cfg = EngineConfig(
            index_repo="owner/index",
            index_token="tok",
            index_branch="main",
            queue_prefix="queue",
            manifest_path="archive_manifest.json",
            telegram_token="bot-token",
            archive_channel_id=-1001,
            admin_ids=frozenset(),
            sqlite_path=Path("/tmp/x.sqlite"),
            data_root=Path("/tmp/data"),
            max_seconds=100,
            day_margin_sec=90,
            stale_sec=900,
            max_archive_steps_per_day=3,
            workflow_repo="owner/code",
            workflow_id="engine.yml",
            github_token_for_dispatch="tok",
        )
        # Sequence: t0=0, then each call advances so remaining < margin before any day work.
        times = iter([0.0, 20.0, 20.0, 20.0, 20.0, 20.0])

        def mono() -> float:
            try:
                return next(times)
            except StopIteration:
                return 20.0

        out = process_request(
            req,
            cfg,
            queue=q,
            notifier=notifier,
            day_runner=lambda *a, **k: {"status": "SUCCESS"},
            archive_day=lambda *a, **k: {"status": "INDEXED"},
            is_indexed=lambda *a, **k: False,
            redispatch=lambda _rid: True,
            monotonic=mono,
        )
        self.assertEqual(out["status"], STATUS_QUEUED)
        self.assertEqual(out.get("next_day"), "2024-06-10")


if __name__ == "__main__":
    unittest.main()
