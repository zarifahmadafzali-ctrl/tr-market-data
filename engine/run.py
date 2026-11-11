"""Core Actions engine loop: claim queue request → day pipeline → archive → continue.

Does not touch Render Telegram polling. Durable state lives in the private index
repo (queue/*.json + archive_manifest.json) and the Telegram channel.
"""

from __future__ import annotations

from pathlib import Path

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from engine.config import EngineConfig, load_config
from engine.github_api import GitHubContents, GitHubError
from engine.notify import Notifier
from engine.queue_store import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_QUEUED,
    STATUS_RUNNING,
    QueueStore,
    utc_now_iso,
)

log = logging.getLogger("engine.run")

DayRunner = Callable[..., dict[str, Any]]
ArchiveDay = Callable[..., dict[str, Any]]
IndexedCheck = Callable[..., bool]
Redispatch = Callable[[str], bool]


def _daterange(start: date, end: date) -> list[date]:
    if end < start:
        return []
    out: list[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def default_day_runner(symbol: str, day: date, *, data_root, db_path, provider: str, force: bool = False) -> dict[str, Any]:
    from processor.pipeline import run_day_pipeline

    return run_day_pipeline(symbol, day, data_root=data_root, db_path=db_path, provider=provider, force=force)


def default_archive_day(
    symbol: str,
    day: str,
    *,
    db_path,
    data_root,
    provider: str,
    max_steps: int,
    requester_chat_id: int | None = None,
    requester_user_id: int | None = None,
) -> dict[str, Any]:
    from bot.archive import enqueue_archive_for_day, package_fully_indexed, process_one_artifact

    enq = enqueue_archive_for_day(
        db_path,
        symbol,
        day,
        provider=provider,
        root=data_root,
        requester_chat_id=requester_chat_id,
        requester_user_id=requester_user_id,
    )
    steps = 0
    last: dict[str, Any] | None = None
    while steps < max_steps:
        if package_fully_indexed(db_path, symbol, day, kind="both", provider=provider):
            return {"status": "INDEXED", "steps": steps, "enqueue": enq, "last": last}
        last = process_one_artifact(db_path)
        steps += 1
        if last is None:
            break
    fully = package_fully_indexed(db_path, symbol, day, kind="both", provider=provider)
    return {
        "status": "INDEXED" if fully else "PARTIAL_ARCHIVE",
        "steps": steps,
        "enqueue": enq,
        "last": last,
    }


def default_is_indexed(symbol: str, day: str, *, db_path, provider: str) -> bool:
    try:
        from bot.archive import package_fully_indexed

        return package_fully_indexed(db_path, symbol, day, kind="both", provider=provider)
    except Exception:  # noqa: BLE001
        return False


def default_redispatch(cfg: EngineConfig, request_id: str) -> bool:
    """Trigger workflow_dispatch so another Actions job continues the queue."""
    if not cfg.github_token_for_dispatch or not cfg.workflow_repo:
        log.warning("redispatch skipped: missing token or workflow repo")
        return False
    import json
    import urllib.error
    import urllib.request

    url = f"https://api.github.com/repos/{cfg.workflow_repo}/actions/workflows/{cfg.workflow_id}/dispatches"
    body = json.dumps({"ref": "main", "inputs": {"request_id": request_id}}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {cfg.github_token_for_dispatch}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tr-market-engine",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            ok = 200 <= resp.status < 300
            log.info("workflow redispatch status=%s request_id=%s", resp.status, request_id)
            return ok
    except urllib.error.HTTPError as e:
        log.warning("workflow redispatch HTTP %s: %s", e.code, e.read()[:200])
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("workflow redispatch failed: %s", e)
        return False


def process_request(
    req: dict[str, Any],
    cfg: EngineConfig,
    *,
    queue: QueueStore,
    notifier: Notifier,
    day_runner: DayRunner = default_day_runner,
    archive_day: ArchiveDay = default_archive_day,
    is_indexed: IndexedCheck = default_is_indexed,
    redispatch: Redispatch | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run one queue request day-by-day until done, failed, or time budget exhausted."""
    rid = str(req["id"])
    symbol = str(req["symbol"])
    start = _parse_day(str(req["start"]))
    end = _parse_day(str(req["end"]))
    days = _daterange(start, end)
    next_day_hint = req.get("next_day")
    if next_day_hint:
        try:
            nd = _parse_day(str(next_day_hint))
            days = [d for d in days if d >= nd]
        except ValueError:
            pass

    t0 = monotonic()
    deadline = t0 + cfg.max_seconds
    progress: dict[str, Any] = {
        "done_days": list(req.get("done_days") or []),
        "failed_days": list(req.get("failed_days") or []),
        "skipped_days": list(req.get("skipped_days") or []),
        "empty_days": list(req.get("empty_days") or list((req.get("progress") or {}).get("empty_days") or [])),
    }
    # Prefer nested progress from queue file if present
    if isinstance(req.get("progress"), dict):
        for key in ("done_days", "failed_days", "skipped_days", "empty_days"):
            if req["progress"].get(key):
                progress[key] = list(req["progress"][key])

    def heartbeat(extra: dict[str, Any] | None = None) -> None:
        def mut(r: dict) -> dict:
            r = dict(r)
            r["status"] = STATUS_RUNNING
            r["heartbeat_at"] = utc_now_iso()
            r["progress"] = progress
            if extra:
                r.update(extra)
            return r

        try:
            queue.update(rid, mut, message=f"queue: heartbeat {rid}")
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat failed: %s", e)

    def mark_running() -> None:
        def mut(r: dict) -> dict:
            r = dict(r)
            r["status"] = STATUS_RUNNING
            r["started_at"] = r.get("started_at") or utc_now_iso()
            r["heartbeat_at"] = utc_now_iso()
            r["attempts"] = int(r.get("attempts") or 0) + 1
            r["progress"] = progress
            return r

        queue.update(rid, mut, message=f"queue: running {rid}")

    mark_running()
    chat = req.get("requester_chat_id")
    if isinstance(chat, str) and chat.strip().lstrip("-").isdigit():
        chat = int(chat.strip())
    elif isinstance(chat, float):
        chat = int(chat)
    notifier.send_message(
        chat if isinstance(chat, int) else None,
        f"Engine started: {symbol} {start.isoformat()}..{end.isoformat()} ({len(days)} day(s) left)",
    )

    for day in days:
        remaining = deadline - monotonic()
        if remaining < cfg.day_margin_sec:
            log.info("time budget low (%.0fs) — pausing before %s", remaining, day)
            def mut_pause(r: dict) -> dict:
                r = dict(r)
                r["status"] = STATUS_QUEUED
                r["next_day"] = day.isoformat()
                r["progress"] = progress
                r["heartbeat_at"] = utc_now_iso()
                r["pause_reason"] = "time_budget"
                return r

            queue.update(rid, mut_pause, message=f"queue: pause {rid} at {day}")
            if redispatch:
                redispatch(rid)
            notifier.send_message(
                chat if isinstance(chat, int) else None,
                f"Paused (time budget). Will continue from {day.isoformat()}.",
            )
            return {"status": STATUS_QUEUED, "next_day": day.isoformat(), "progress": progress}

        day_s = day.isoformat()
        if (
            day_s in progress["done_days"]
            or day_s in progress["skipped_days"]
            or day_s in progress.get("empty_days", [])
        ):
            continue

        heartbeat({"current_day": day_s})
        try:
            if is_indexed(symbol, day_s, db_path=cfg.sqlite_path, provider=cfg.provider):
                progress["skipped_days"].append(day_s)
                log.info("already INDEXED %s %s — skip", symbol, day_s)
                continue

            result = day_runner(
                symbol,
                day,
                data_root=cfg.data_root,
                db_path=cfg.sqlite_path,
                provider=cfg.provider,
                force=False,
            )
            status = str(result.get("status") or "")
            if status == "SUCCESS":
                arch = archive_day(
                    symbol,
                    day_s,
                    db_path=cfg.sqlite_path,
                    data_root=cfg.data_root,
                    provider=cfg.provider,
                    max_steps=cfg.max_archive_steps_per_day,
                    requester_chat_id=chat if isinstance(chat, int) else None,
                    requester_user_id=req.get("requester_user_id")
                    if isinstance(req.get("requester_user_id"), int)
                    else None,
                )
                log.info("day %s download=%s archive=%s", day_s, status, arch.get("status"))
                if day_s not in progress["done_days"]:
                    progress["done_days"].append(day_s)
                if day_s in progress["failed_days"]:
                    progress["failed_days"] = [d for d in progress["failed_days"] if d != day_s]
            elif status == "EXPECTED_EMPTY":
                # Weekend / holiday — not a failure; durable skip on resume
                log.info("day %s status=EXPECTED_EMPTY (no market data)", day_s)
                progress.setdefault("empty_days", [])
                if day_s not in progress["empty_days"]:
                    progress["empty_days"].append(day_s)
                if day_s in progress["failed_days"]:
                    progress["failed_days"] = [d for d in progress["failed_days"] if d != day_s]
            else:
                log.warning("day %s status=%s", day_s, status)
                if day_s not in progress["failed_days"]:
                    progress["failed_days"].append(day_s)
        except Exception as e:  # noqa: BLE001
            log.exception("day %s failed: %s", day_s, e)
            progress["failed_days"].append(day_s)
        heartbeat({"current_day": day_s})

    # Second pass: retry failed days once within the same Actions job if time allows
    retry_list = list(progress.get("failed_days") or [])
    if retry_list and (deadline - monotonic()) >= cfg.day_margin_sec:
        log.info("retry pass for %s failed day(s): %s", len(retry_list), retry_list)
        still_failed: list[str] = []
        for day_s in retry_list:
            remaining = deadline - monotonic()
            if remaining < cfg.day_margin_sec:
                still_failed.extend(
                    d for d in retry_list[retry_list.index(day_s) :] if d not in still_failed
                )
                break
            try:
                day = _parse_day(day_s)
                if is_indexed(symbol, day_s, db_path=cfg.sqlite_path, provider=cfg.provider):
                    progress["done_days"].append(day_s)
                    continue
                result = day_runner(
                    symbol,
                    day,
                    data_root=cfg.data_root,
                    db_path=cfg.sqlite_path,
                    provider=cfg.provider,
                    force=False,
                )
                status = str(result.get("status") or "")
                if status == "SUCCESS":
                    arch = archive_day(
                        symbol,
                        day_s,
                        db_path=cfg.sqlite_path,
                        data_root=cfg.data_root,
                        provider=cfg.provider,
                        max_steps=cfg.max_archive_steps_per_day,
                        requester_chat_id=chat if isinstance(chat, int) else None,
                        requester_user_id=req.get("requester_user_id")
                        if isinstance(req.get("requester_user_id"), int)
                        else None,
                    )
                    log.info("retry day %s download=%s archive=%s", day_s, status, arch.get("status"))
                    progress["done_days"].append(day_s)
                elif status == "EXPECTED_EMPTY":
                    progress.setdefault("empty_days", [])
                    if day_s not in progress["empty_days"]:
                        progress["empty_days"].append(day_s)
                else:
                    still_failed.append(day_s)
            except Exception as e:  # noqa: BLE001
                log.exception("retry day %s failed: %s", day_s, e)
                still_failed.append(day_s)
            heartbeat({"current_day": day_s, "phase": "retry"})
        progress["failed_days"] = still_failed

    # Finished range — if failures remain, re-queue for a later Actions run (preserve progress)
    if progress["failed_days"] and not progress["done_days"] and not progress["skipped_days"] and not progress.get("empty_days"):
        final = STATUS_FAILED
    elif progress["failed_days"]:
        final = STATUS_PARTIAL
    else:
        final = STATUS_DONE

    def mut_done(r: dict) -> dict:
        r = dict(r)
        r["progress"] = progress
        r.pop("pause_reason", None)
        if final == STATUS_PARTIAL and progress.get("failed_days"):
            # Keep durable progress; make request runnable again for failed days only
            r["status"] = STATUS_QUEUED
            r["next_day"] = progress["failed_days"][0]
            r["finished_at"] = None
            r["requeue_reason"] = "retry_failed_days"
        else:
            r["status"] = final
            r["finished_at"] = utc_now_iso()
            r.pop("next_day", None)
            r.pop("requeue_reason", None)
        return r

    queue.update(rid, mut_done, message=f"queue: {final} {rid}")
    if final == STATUS_PARTIAL and progress.get("failed_days") and redispatch:
        redispatch(rid)
        log.info("redispatched request %s to retry %s failed day(s)", rid, len(progress["failed_days"]))
    notifier.send_message(
        chat if isinstance(chat, int) else None,
        f"Engine {final}: {symbol} done={len(progress['done_days'])} "
        f"empty={len(progress.get('empty_days') or [])} "
        f"skipped={len(progress['skipped_days'])} failed={len(progress['failed_days'])}"
        + (
            f" (requeued for retry from {progress['failed_days'][0]})"
            if final == STATUS_PARTIAL and progress.get("failed_days")
            else ""
        ),
    )

    # Ephemeral range ZIP delivery (daily archive remains source of truth)
    pack_result: dict[str, Any] | None = None
    deliverable = list(progress.get("done_days") or []) + list(progress.get("skipped_days") or [])
    if (
        isinstance(chat, int)
        and cfg.telegram_token
        and deliverable
        and final in (STATUS_DONE, STATUS_PARTIAL, STATUS_FAILED)
    ):
        try:
            from engine.range_package import deliver_range_package

            kind = str(req.get("kind") or "both")
            manifest_obj = None
            try:
                from bot.manifest import fetch_manifest

                manifest_obj = fetch_manifest()
            except Exception as e:  # noqa: BLE001
                log.warning("manifest load for packaging: %s", e)

            pack_result = deliver_range_package(
                symbol=symbol,
                start=start.isoformat(),
                end=end.isoformat(),
                kind=kind,
                progress=progress,
                data_root=Path(cfg.data_root),
                db_path=Path(cfg.sqlite_path),
                token=cfg.telegram_token,
                chat_id=chat,
                manifest=manifest_obj if isinstance(manifest_obj, dict) else None,
                provider=cfg.provider,
                send_document_path=lambda cid, path, cap: notifier.send_document_path(cid, path, cap),
                send_message=lambda cid, text: notifier.send_message(cid, text),
            )
            log.info("range package result=%s", pack_result.get("status") if pack_result else None)
        except Exception as e:  # noqa: BLE001
            log.exception("range package failed: %s", e)
            notifier.send_message(
                chat,
                f"Range ZIP packaging failed: {e}. Day archives remain available by single-day /get.",
            )

    out: dict[str, Any] = {"status": final, "progress": progress}
    if pack_result is not None:
        out["package"] = pack_result
    return out


def run_once(
    cfg: EngineConfig | None = None,
    *,
    request_id_hint: str | None = None,
    day_runner: DayRunner | None = None,
    archive_day: ArchiveDay | None = None,
    is_indexed: IndexedCheck | None = None,
    gh: GitHubContents | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Claim one runnable request (if any) and process it."""
    cfg = cfg or load_config()
    if not cfg.index_repo or not cfg.index_token:
        return {"status": "misconfigured", "error": "missing index repo/token"}

    gh = gh or GitHubContents(cfg.index_repo, cfg.index_token, branch=cfg.index_branch)
    queue = QueueStore(gh, prefix=cfg.queue_prefix)
    notifier = notifier or Notifier(cfg.telegram_token)

    # Best-effort: rebuild local SQLite catalog from manifest before work
    try:
        from bot.manifest import startup_sync_manifest

        sync = startup_sync_manifest(cfg.sqlite_path)
        log.info("manifest sync status=%s", sync.get("status"))
    except Exception as e:  # noqa: BLE001
        log.warning("manifest sync skipped: %s", e)

    req = queue.next_runnable(stale_sec=cfg.stale_sec, hint=request_id_hint)
    if not req:
        return {"status": "idle", "message": "no runnable queue requests"}

    rid = str(req["id"])
    log.info("claimed request %s %s %s..%s", rid, req.get("symbol"), req.get("start"), req.get("end"))

    def _rd(request_id: str) -> bool:
        return default_redispatch(cfg, request_id)

    return process_request(
        req,
        cfg,
        queue=queue,
        notifier=notifier,
        day_runner=day_runner or default_day_runner,
        archive_day=archive_day or default_archive_day,
        is_indexed=is_indexed or default_is_indexed,
        redispatch=_rd,
    )


def main_argv(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="TR Market Data Actions engine")
    p.add_argument("--request-id", default=None, help="Prefer this queue id if runnable")
    p.add_argument("--once", action="store_true", help="Process at most one request (default)")
    args = p.parse_args(argv)
    cfg = load_config()
    if not cfg.ready:
        log.error("Engine misconfigured: need TELEGRAM_BOT_TOKEN + ARCHIVE_MANIFEST_REPO + token")
        return 2
    out = run_once(cfg, request_id_hint=args.request_id)
    log.info("engine result: %s", out)
    st = out.get("status")
    if st in (STATUS_DONE, STATUS_PARTIAL, "idle", STATUS_QUEUED):
        return 0
    if st == "misconfigured":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main_argv())
