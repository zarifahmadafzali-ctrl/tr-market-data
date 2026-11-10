"""GitHub-backed SQLite catalog mirror for ephemeral Render disks.

NOT a substitute for Telegram blob storage. Only the small metadata DB is mirrored:
job queue, download_days status, archive INDEXED file_ids.

Safety:
- Uses sqlite3 backup API (consistent snapshot) — never uploads a live-locked file mid-write.
- Upload is rate-limited; binary commits can bloat git history if done too often.
- Startup download is best-effort: failure does not crash the bot (empty local DB is OK).
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from bot.config import (
    sqlite_backup_branch,
    sqlite_backup_enabled,
    sqlite_backup_max_bytes,
    sqlite_backup_min_interval_sec,
    sqlite_backup_path,
    sqlite_backup_repo,
    sqlite_backup_token,
    sqlite_path,
)

log = logging.getLogger("bot.sqlite_backup")

_lock = threading.Lock()
_last_upload_mono = 0.0
_last_upload_ok = False


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tr-market-data-sqlite-backup",
    }


def _contents_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path.lstrip('/')}"


def consistent_snapshot(db_path: Path, dest: Path) -> int:
    """Copy DB via sqlite backup API into dest. Returns byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return dest.stat().st_size


def fetch_remote_sqlite(
    *,
    dest: Path,
    token: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Download remote sqlite into dest. status: ok|missing|error|disabled|misconfigured."""
    if not sqlite_backup_enabled() and token is None:
        if not (token or sqlite_backup_token()):
            return {"status": "disabled"}

    tok = token if token is not None else sqlite_backup_token()
    r = repo if repo is not None else sqlite_backup_repo()
    p = path if path is not None else sqlite_backup_path()
    b = branch if branch is not None else sqlite_backup_branch()
    if not tok or not r:
        return {"status": "misconfigured"}

    url = _contents_url(r, p)
    own = client is None
    cli = client or httpx.Client(timeout=60.0)
    try:
        resp = cli.get(url, headers=_headers(tok), params={"ref": b})
        if resp.status_code == 404:
            return {"status": "missing"}
        if resp.status_code in (401, 403):
            log.warning("SQLite backup fetch unauthorized (token/repo scopes)")
            return {"status": "error", "http": resp.status_code}
        if resp.status_code != 200:
            log.warning("SQLite backup fetch HTTP %s", resp.status_code)
            return {"status": "error", "http": resp.status_code}
        payload = resp.json()
        content_b64 = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            raw = base64.b64decode(content_b64.replace("\n", ""))
        else:
            raw = content_b64.encode("utf-8") if isinstance(content_b64, str) else bytes(content_b64)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        return {
            "status": "ok",
            "bytes": len(raw),
            "sha": payload.get("sha"),
            "path": str(dest),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("SQLite backup fetch failed: %s", e)
        return {"status": "error", "error": str(e)}
    finally:
        if own:
            cli.close()


def put_remote_sqlite(
    local_file: Path,
    *,
    token: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    message: str = "backup: sqlite catalog",
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Upload local_file bytes to GitHub Contents API (create or update)."""
    tok = token if token is not None else sqlite_backup_token()
    r = repo if repo is not None else sqlite_backup_repo()
    p = path if path is not None else sqlite_backup_path()
    b = branch if branch is not None else sqlite_backup_branch()
    if not tok or not r:
        return {"status": "misconfigured"}
    if not local_file.is_file():
        return {"status": "error", "error": "missing_local_file"}

    size = local_file.stat().st_size
    max_b = sqlite_backup_max_bytes()
    if size > max_b:
        log.warning("SQLite backup skipped: size %s > max %s", size, max_b)
        return {"status": "too_large", "bytes": size, "max": max_b}

    data = local_file.read_bytes()
    body_base = {
        "message": message,
        "content": base64.b64encode(data).decode("ascii"),
        "branch": b,
    }

    url = _contents_url(r, p)
    own = client is None
    cli = client or httpx.Client(timeout=120.0)
    try:
        # Get current sha if file exists
        sha = None
        meta = cli.get(url, headers=_headers(tok), params={"ref": b})
        if meta.status_code == 200:
            sha = meta.json().get("sha")
        elif meta.status_code not in (404, 200):
            log.warning("SQLite backup meta HTTP %s", meta.status_code)

        for attempt in range(4):
            body = dict(body_base)
            if sha:
                body["sha"] = sha
            resp = cli.put(url, headers=_headers(tok), json=body)
            if resp.status_code in (200, 201):
                new_sha = (resp.json().get("content") or {}).get("sha")
                return {"status": "ok", "bytes": size, "sha": new_sha, "attempt": attempt + 1}
            if resp.status_code in (409, 422):
                # Stale sha — refresh and retry
                meta2 = cli.get(url, headers=_headers(tok), params={"ref": b})
                if meta2.status_code == 200:
                    sha = meta2.json().get("sha")
                    continue
                if meta2.status_code == 404:
                    sha = None
                    continue
            log.warning("SQLite backup put HTTP %s", resp.status_code)
            return {"status": "error", "http": resp.status_code}
        return {"status": "error", "error": "conflict_retries_exhausted"}
    except Exception as e:  # noqa: BLE001
        log.warning("SQLite backup put failed: %s", e)
        return {"status": "error", "error": str(e)}
    finally:
        if own:
            cli.close()


def restore_sqlite_from_github(db_path: Path | None = None) -> dict[str, Any]:
    """Startup: pull remote DB over local path if enabled. Never raises to caller for transient errors."""
    if not sqlite_backup_enabled():
        return {"status": "disabled"}
    path = Path(db_path) if db_path else sqlite_path()
    # If local already has a non-trivial DB (e.g. persistent disk), do not overwrite
    # unless SQLITE_BACKUP_FORCE_RESTORE=true
    import os

    force = (os.getenv("SQLITE_BACKUP_FORCE_RESTORE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if path.is_file() and path.stat().st_size > 4096 and not force:
        log.info(
            "SQLite backup restore skipped: local DB exists (%s bytes). Set SQLITE_BACKUP_FORCE_RESTORE=true to overwrite.",
            path.stat().st_size,
        )
        return {"status": "skipped_local_exists", "bytes": path.stat().st_size}

    result = fetch_remote_sqlite(dest=path)
    if result.get("status") == "ok":
        log.info("SQLite restored from GitHub (%s bytes)", result.get("bytes"))
    elif result.get("status") == "missing":
        log.info("No remote SQLite backup yet — starting with empty catalog")
    else:
        log.warning("SQLite restore status=%s", result.get("status"))
    return result


def push_sqlite_to_github(
    db_path: Path | None = None,
    *,
    reason: str = "catalog",
    force: bool = False,
) -> dict[str, Any]:
    """Snapshot local DB and upload. Rate-limited unless force=True."""
    global _last_upload_mono, _last_upload_ok
    if not sqlite_backup_enabled():
        return {"status": "disabled"}
    if not sqlite_backup_token() or not sqlite_backup_repo():
        return {"status": "misconfigured"}

    path = Path(db_path) if db_path else sqlite_path()
    if not path.is_file():
        return {"status": "error", "error": "no_local_db"}

    with _lock:
        now = time.monotonic()
        min_iv = sqlite_backup_min_interval_sec()
        if not force and _last_upload_mono and (now - _last_upload_mono) < min_iv:
            return {
                "status": "rate_limited",
                "retry_in_sec": int(min_iv - (now - _last_upload_mono)),
            }

        tmp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
                tmp = Path(f.name)
            size = consistent_snapshot(path, tmp)
            msg = f"backup: sqlite catalog ({reason}, {size} bytes)"
            result = put_remote_sqlite(tmp, message=msg)
            if result.get("status") == "ok":
                _last_upload_mono = time.monotonic()
                _last_upload_ok = True
                log.info("SQLite backup uploaded reason=%s bytes=%s", reason, size)
            else:
                log.warning("SQLite backup upload status=%s", result)
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("SQLite backup push failed: %s", e)
            return {"status": "error", "error": str(e)}
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass


def maybe_periodic_backup(db_path: Path | None = None) -> dict[str, Any] | None:
    """Called from worker loop; no-op when disabled or rate-limited."""
    if not sqlite_backup_enabled():
        return None
    return push_sqlite_to_github(db_path, reason="periodic")
