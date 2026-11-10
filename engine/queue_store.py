"""Durable request queue: one JSON file per request under queue/ in the private index repo."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from engine.github_api import ConflictError, GitHubContents

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL = {STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED}
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def valid_id(rid: str) -> bool:
    return bool(_ID_RE.match(rid or ""))


def new_request(
    *,
    symbol: str,
    start: str,
    end: str,
    kind: str = "both",
    requester_chat_id: int | None = None,
    requester_user_id: int | None = None,
) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "created_at": utc_now_iso(),
        "requester_chat_id": requester_chat_id,
        "requester_user_id": requester_user_id,
        "symbol": symbol,
        "start": start,
        "end": end,
        "kind": kind,
        "status": STATUS_QUEUED,
        "attempts": 0,
    }


class QueueStore:
    def __init__(self, gh: GitHubContents, prefix: str = "queue", now: Callable[[], datetime] | None = None):
        self.gh = gh
        self.prefix = prefix.strip("/")
        self.now = now or (lambda: datetime.now(timezone.utc))

    def _path(self, rid: str) -> str:
        if not valid_id(rid):
            raise ValueError(f"invalid request id: {rid!r}")
        return f"{self.prefix}/{rid}.json"

    def create(self, req: dict[str, Any]) -> dict[str, Any]:
        self.gh.put_json(self._path(req["id"]), req, None, f"queue: add {req['id']} {req['symbol']} {req['start']}..{req['end']}")
        return req

    def get(self, rid: str) -> "tuple[dict | None, str | None]":
        return self.gh.get_json(self._path(rid))

    def update(self, rid: str, mutate: Callable[[dict], dict], *, retries: int = 4, message: str | None = None) -> dict[str, Any]:
        """Read-modify-write with retry on conflict. Returns the stored request."""
        last: Exception | None = None
        for _ in range(retries):
            req, sha = self.get(rid)
            if req is None:
                raise KeyError(f"request {rid} not found")
            new = mutate(dict(req))
            try:
                self.gh.put_json(self._path(rid), new, sha, message or f"queue: {rid} -> {new.get('status')}")
                return new
            except ConflictError as e:
                last = e
        raise ConflictError(f"update of {rid} kept conflicting: {last}")

    def list_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for entry in self.gh.list_dir(self.prefix)[:limit]:
            name = str(entry.get("name", ""))
            if not name.endswith(".json"):
                continue
            rid = name[:-5]
            if not valid_id(rid):
                continue
            req, _sha = self.get(rid)
            if req:
                out.append(req)
        return out

    def next_runnable(self, *, stale_sec: int, hint: str | None = None) -> dict[str, Any] | None:
        """Oldest queued request (hint first). A `running` request whose heartbeat is older than
        stale_sec belongs to a dead run and is runnable again (stale_sec=0 -> any running one)."""
        now = self.now()
        runnable: list[dict[str, Any]] = []
        for r in self.list_requests():
            st = r.get("status")
            if st == STATUS_QUEUED:
                runnable.append(r)
            elif st == STATUS_RUNNING:
                hb = _parse(r.get("heartbeat_at")) or _parse(r.get("started_at"))
                if hb is None or now - hb >= timedelta(seconds=stale_sec):
                    runnable.append(r)
        if not runnable:
            return None
        runnable.sort(key=lambda r: (r.get("id") != hint, r.get("created_at", "")))
        return runnable[0]

    def prune_finished(self, older_than_sec: int = 7 * 86400) -> int:
        """Delete finished request files older than the cutoff. Returns count removed."""
        removed = 0
        now = self.now()
        for entry in self.gh.list_dir(self.prefix):
            name = str(entry.get("name", ""))
            rid = name[:-5] if name.endswith(".json") else ""
            if not valid_id(rid):
                continue
            req, sha = self.get(rid)
            if not req or req.get("status") not in TERMINAL or not sha:
                continue
            fin = _parse(req.get("finished_at"))
            if fin and now - fin >= timedelta(seconds=older_than_sec):
                try:
                    self.gh.delete(self._path(rid), sha, f"queue: prune {rid}")
                    removed += 1
                except ConflictError:
                    continue
        return removed
