"""Durable Telegram channel archive for market-data artifacts.

SQLite holds metadata + Telegram file references only.
Local disk is staging. The private channel holds the blobs.

Uploads are at-least-once: a crash after Telegram accepts a file but before
SQLite INDEXED may produce a duplicate channel message on retry.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from bot.config import (
    TELEGRAM_MAX_UPLOAD_BYTES,
    allow_delete_staging,
    archive_backoff_base_sec,
    archive_backoff_cap_sec,
    archive_channel_id,
    archive_enabled,
    archive_max_retries,
    data_root,
    sqlite_path,
    telegram_token,
)
from db.catalog import connect, get_day, utc_now
from downloader.symbols import normalize_symbol

log = logging.getLogger("bot.archive")

ARTIFACT_TICKS = "ticks_parquet"
ARTIFACT_BARS_1S = "bars_1s_parquet"
ARTIFACT_META = "metadata_json"

ARTIFACT_FILE_NAMES = {
    ARTIFACT_TICKS: "ticks.parquet",
    ARTIFACT_BARS_1S: "bars_1s.parquet",
    ARTIFACT_META: "metadata.json",
}

STATUS_PENDING = "PENDING"
STATUS_UPLOADING = "UPLOADING"
STATUS_UPLOADED = "UPLOADED"
STATUS_INDEXED = "INDEXED"
STATUS_FAILED = "FAILED"
STATUS_PARTIAL = "PARTIAL"
STATUS_CANCELLED = "CANCELLED"

# Upload transport injectable for tests
UploadFn = Callable[[Path, str, int, str], dict[str, Any]]


def package_key(provider: str, symbol: str, day: str) -> str:
    return f"{provider}|{normalize_symbol(symbol)}|{day}"


def artifact_key(provider: str, symbol: str, day: str, artifact_type: str) -> str:
    return f"{provider}|{normalize_symbol(symbol)}|{day}|{artifact_type}"


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def backoff_seconds(retry_count: int, base: int | None = None, cap: int | None = None) -> int:
    b = base if base is not None else archive_backoff_base_sec()
    c = cap if cap is not None else archive_backoff_cap_sec()
    delay = b * (2 ** max(0, retry_count))
    return int(min(c, delay))


def next_attempt_iso(retry_count: int) -> str:
    delay = backoff_seconds(retry_count)
    return (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=delay)
    ).isoformat()


def _resolve_local(path_str: str | None, root: Path) -> Path | None:
    if not path_str:
        return None
    raw = Path(path_str)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(root / raw)
        parts = raw.parts
        if parts and parts[0] == "data":
            candidates.append(root / Path(*parts[1:]))
        candidates.append(root.parent / raw)
    for cand in candidates:
        try:
            if cand.is_file():
                return cand.resolve()
        except Exception:
            continue
    return None


@dataclass
class LocalArtifact:
    artifact_type: str
    path: Path
    file_name: str
    size_bytes: int
    sha256: str


def discover_local_artifacts(
    db_path: Path,
    symbol: str,
    day: str,
    *,
    provider: str = "dukascopy",
    root: Path | None = None,
) -> list[LocalArtifact]:
    """Return only artifacts that exist on disk for this catalog day."""
    root = root or data_root()
    sym = normalize_symbol(symbol)
    conn = connect(db_path)
    row = get_day(conn, sym, day, provider)
    if not row:
        return []

    mapping = [
        (ARTIFACT_TICKS, row.get("tick_file_path")),
        (ARTIFACT_BARS_1S, row.get("bars_1s_file_path")),
        (ARTIFACT_META, row.get("metadata_path")),
    ]
    out: list[LocalArtifact] = []
    for atype, path_str in mapping:
        path = _resolve_local(path_str, root)
        if path is None or not path.is_file():
            continue
        size = path.stat().st_size
        if size <= 0:
            continue
        digest = sha256_file(path)
        out.append(
            LocalArtifact(
                artifact_type=atype,
                path=path,
                file_name=ARTIFACT_FILE_NAMES.get(atype, path.name),
                size_bytes=size,
                sha256=digest,
            )
        )
    return out


def enqueue_archive_for_day(
    db_path: Path,
    symbol: str,
    day: str,
    *,
    provider: str = "dukascopy",
    root: Path | None = None,
    requester_chat_id: int | None = None,
    requester_user_id: int | None = None,
) -> dict[str, Any]:
    """Create package + artifact rows for files present on disk. Idempotent."""
    root = root or data_root()
    sym = normalize_symbol(symbol)
    pk = package_key(provider, sym, day)
    locals_ = discover_local_artifacts(db_path, sym, day, provider=provider, root=root)
    conn = connect(db_path)
    now = utc_now()
    max_r = archive_max_retries()

    cur = conn.execute("SELECT * FROM archive_jobs WHERE package_key = ?", (pk,))
    job = cur.fetchone()
    if job is None:
        if not locals_:
            return {"package_key": pk, "job_id": None, "enqueued": 0, "reason": "no_local_files"}
        conn.execute(
            """
            INSERT INTO archive_jobs (
              package_key, symbol, provider, day, status,
              artifact_expected, artifact_indexed, retry_count, max_retries,
              requester_chat_id, requester_user_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?)
            """,
            (
                pk,
                sym,
                provider,
                day,
                STATUS_PENDING,
                len(locals_),
                max_r,
                requester_chat_id,
                requester_user_id,
                now,
                now,
            ),
        )
        job_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        job_id = int(job["id"])
        if requester_chat_id is not None or requester_user_id is not None:
            conn.execute(
                """
                UPDATE archive_jobs SET
                  requester_chat_id = COALESCE(?, requester_chat_id),
                  requester_user_id = COALESCE(?, requester_user_id),
                  updated_at = ?
                WHERE id = ?
                """,
                (requester_chat_id, requester_user_id, now, job_id),
            )

    enqueued = 0
    for art in locals_:
        ak = artifact_key(provider, sym, day, art.artifact_type)
        existing = conn.execute(
            "SELECT id, status FROM archived_artifacts WHERE artifact_key = ?", (ak,)
        ).fetchone()
        if existing:
            if existing["status"] == STATUS_INDEXED:
                continue
            # Refresh local path/checksum if still pending
            conn.execute(
                """
                UPDATE archived_artifacts SET
                  local_path = ?, size_bytes = ?, sha256 = ?,
                  updated_at = ?
                WHERE id = ? AND status != ?
                """,
                (str(art.path), art.size_bytes, art.sha256, now, int(existing["id"]), STATUS_INDEXED),
            )
            continue
        conn.execute(
            """
            INSERT INTO archived_artifacts (
              archive_job_id, artifact_key, symbol, provider, day, artifact_type,
              file_name, local_path, size_bytes, sha256, status,
              retry_count, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                job_id,
                ak,
                sym,
                provider,
                day,
                art.artifact_type,
                art.file_name,
                str(art.path),
                art.size_bytes,
                art.sha256,
                STATUS_PENDING,
                max_r,
                now,
                now,
            ),
        )
        enqueued += 1

    # Update expected count from actual non-deleted rows
    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM archived_artifacts WHERE archive_job_id = ?",
        (job_id,),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE archive_jobs SET artifact_expected = ?, updated_at = ? WHERE id = ?",
        (int(cnt), now, job_id),
    )
    conn.commit()
    recompute_package_status(conn, job_id)
    conn.commit()
    return {"package_key": pk, "job_id": job_id, "enqueued": enqueued, "artifacts": cnt}


def recompute_package_status(conn: sqlite3.Connection, job_id: int) -> str:
    rows = conn.execute(
        "SELECT status FROM archived_artifacts WHERE archive_job_id = ?",
        (job_id,),
    ).fetchall()
    if not rows:
        status = STATUS_PENDING
        indexed = 0
    else:
        statuses = [r["status"] for r in rows]
        indexed = sum(1 for s in statuses if s == STATUS_INDEXED)
        if indexed == len(statuses) and indexed > 0:
            status = STATUS_INDEXED
        elif indexed > 0:
            status = STATUS_PARTIAL
        elif all(s == STATUS_FAILED for s in statuses):
            status = STATUS_FAILED
        elif any(s == STATUS_UPLOADING for s in statuses):
            status = STATUS_UPLOADING
        else:
            status = STATUS_PENDING
    conn.execute(
        """
        UPDATE archive_jobs SET
          status = ?, artifact_indexed = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, indexed, utc_now(), job_id),
    )
    return status


def recover_stale_uploading(db_path: Path) -> int:
    """UPLOADING without proven RESPONSE_OK → PENDING. RESPONSE_OK → finish INDEXED."""
    conn = connect(db_path)
    now = utc_now()
    rows = conn.execute(
        "SELECT * FROM archived_artifacts WHERE status = ?",
        (STATUS_UPLOADING,),
    ).fetchall()
    n = 0
    for r in rows:
        aid = int(r["id"])
        attempt = conn.execute(
            """
            SELECT * FROM archive_upload_attempts
            WHERE artifact_id = ? AND status = 'RESPONSE_OK'
            ORDER BY id DESC LIMIT 1
            """,
            (aid,),
        ).fetchone()
        if attempt and attempt["telegram_file_id"]:
            _mark_indexed(
                conn,
                aid,
                file_id=attempt["telegram_file_id"],
                file_unique_id=None,
                message_id=attempt["telegram_message_id"],
                chat_id=archive_channel_id(),
            )
            n += 1
        else:
            conn.execute(
                """
                UPDATE archived_artifacts SET
                  status = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (STATUS_PENDING, "recovered stale UPLOADING after restart", now, aid),
            )
            n += 1
        recompute_package_status(conn, int(r["archive_job_id"]))
    conn.commit()
    return n


def _mark_indexed(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    file_id: str,
    file_unique_id: str | None,
    message_id: int | None,
    chat_id: int | None,
) -> None:
    now = utc_now()
    conn.execute(
        """
        UPDATE archived_artifacts SET
          status = ?,
          telegram_file_id = ?,
          telegram_file_unique_id = COALESCE(?, telegram_file_unique_id),
          telegram_message_id = ?,
          telegram_chat_id = ?,
          uploaded_at = COALESCE(uploaded_at, ?),
          indexed_at = ?,
          last_error = NULL,
          updated_at = ?
        WHERE id = ?
        """,
        (
            STATUS_INDEXED,
            file_id,
            file_unique_id,
            message_id,
            chat_id,
            now,
            now,
            now,
            artifact_id,
        ),
    )


def claim_next_artifact(conn: sqlite3.Connection) -> sqlite3.Row | None:
    now = utc_now()
    row = conn.execute(
        """
        SELECT * FROM archived_artifacts
        WHERE status IN (?, ?, ?)
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
          AND retry_count <= max_retries
        ORDER BY id ASC
        LIMIT 1
        """,
        (STATUS_PENDING, STATUS_FAILED, STATUS_UPLOADED, now),
    ).fetchone()
    if not row:
        return None
    # Permanent size failures stay FAILED with high retry — skip if last_error permanent
    if row["status"] == STATUS_FAILED and row["last_error"] and "exceeds Telegram" in (row["last_error"] or ""):
        return None
    cur = conn.execute(
        """
        UPDATE archived_artifacts SET status = ?, updated_at = ?
        WHERE id = ? AND status IN (?, ?, ?)
        """,
        (STATUS_UPLOADING, now, int(row["id"]), STATUS_PENDING, STATUS_FAILED, STATUS_UPLOADED),
    )
    if cur.rowcount != 1:
        return None
    conn.commit()
    return conn.execute(
        "SELECT * FROM archived_artifacts WHERE id = ?", (int(row["id"]),)
    ).fetchone()


def default_telegram_upload(
    path: Path,
    caption: str,
    chat_id: int,
    token: str,
) -> dict[str, Any]:
    """Sync Bot API sendDocument. Returns document + message metadata."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with path.open("rb") as fh:
        files = {"document": (path.name, fh)}
        data = {"chat_id": str(chat_id), "caption": caption[:1024]}
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, data=data, files=files)
    payload = resp.json()
    if resp.status_code != 200 or not payload.get("ok"):
        desc = payload.get("description") if isinstance(payload, dict) else resp.text
        raise RuntimeError(f"Telegram upload failed: {desc}")
    result = payload["result"]
    doc = result.get("document") or {}
    return {
        "file_id": doc.get("file_id"),
        "file_unique_id": doc.get("file_unique_id"),
        "message_id": result.get("message_id"),
        "chat_id": result.get("chat", {}).get("id", chat_id),
    }



def _finalize_with_manifest(
    conn: sqlite3.Connection,
    *,
    aid: int,
    job_id: int,
    row: sqlite3.Row,
    file_id: str,
    file_unique_id: str | None,
    message_id: int | None,
    chat_id: int | None,
    local: Path | None,
) -> dict[str, Any]:
    """Push GitHub manifest then mark INDEXED. On failure leave UPLOADED for retry."""
    from bot.config import manifest_enabled
    from bot.manifest import upsert_artifact_remote

    file_name = row["file_name"] or ARTIFACT_FILE_NAMES.get(row["artifact_type"], "artifact.bin")
    artifact_payload = {
        "file_id": file_id,
        "file_unique_id": file_unique_id,
        "message_id": message_id,
        "chat_id": str(chat_id) if chat_id is not None else None,
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "status": STATUS_INDEXED,
        "artifact_type": row["artifact_type"],
    }
    try:
        if manifest_enabled():
            ok = upsert_artifact_remote(
                package_key=package_key(row["provider"], row["symbol"], row["day"]),
                provider=row["provider"],
                symbol=row["symbol"],
                day=row["day"],
                file_name=file_name,
                artifact=artifact_payload,
            )
            if not ok:
                now = utc_now()
                new_retry = int(row["retry_count"] or 0) + 1
                conn.execute(
                    """
                    UPDATE archived_artifacts SET
                      status = ?, retry_count = ?, next_attempt_at = ?,
                      last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        STATUS_UPLOADED,
                        new_retry,
                        next_attempt_iso(new_retry),
                        "GitHub manifest update failed — will retry without re-upload",
                        now,
                        aid,
                    ),
                )
                recompute_package_status(conn, job_id)
                conn.commit()
                return {
                    "artifact_id": aid,
                    "status": STATUS_UPLOADED,
                    "file_id": file_id,
                    "error": "manifest_update_failed",
                }
        _mark_indexed(
            conn,
            aid,
            file_id=file_id,
            file_unique_id=file_unique_id,
            message_id=message_id,
            chat_id=chat_id,
        )
        recompute_package_status(conn, job_id)
        conn.commit()
        if allow_delete_staging() and local is not None:
            _maybe_delete_staging(local)
        return {
            "artifact_id": aid,
            "status": STATUS_INDEXED,
            "file_id": file_id,
            "message_id": message_id,
        }
    except Exception as e:  # noqa: BLE001
        log.exception("manifest finalize failed artifact=%s", aid)
        now = utc_now()
        conn.execute(
            """
            UPDATE archived_artifacts SET
              status = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                STATUS_UPLOADED,
                f"manifest finalize error: {e}",
                next_attempt_iso(int(row["retry_count"] or 0) + 1),
                now,
                aid,
            ),
        )
        recompute_package_status(conn, job_id)
        conn.commit()
        return {"artifact_id": aid, "status": STATUS_UPLOADED, "error": str(e)}


def process_one_artifact(
    db_path: Path,
    *,
    upload_fn: UploadFn | None = None,
    token: str | None = None,
    channel_id: int | None = None,
) -> dict[str, Any] | None:
    """Claim and process a single archive artifact. Sequential by design."""
    if not archive_enabled() and upload_fn is None:
        # allow tests to inject upload_fn even if disabled
        if channel_id is None:
            return None

    ch = channel_id if channel_id is not None else archive_channel_id()
    tok = token if token is not None else telegram_token()
    if ch is None:
        log.warning("TELEGRAM_ARCHIVE_CHANNEL_ID not set — archive worker idle")
        return None
    if not tok and upload_fn is None:
        log.warning("TELEGRAM_BOT_TOKEN missing — cannot upload")
        return None

    conn = connect(db_path)
    row = claim_next_artifact(conn)
    if not row:
        return None

    aid = int(row["id"])
    job_id = int(row["archive_job_id"])
    local = Path(row["local_path"]) if row["local_path"] else None
    attempt_no = int(row["retry_count"]) + 1
    now = utc_now()

    conn.execute(
        """
        INSERT INTO archive_upload_attempts (
          artifact_id, attempt_no, status, local_sha256, started_at
        ) VALUES (?, ?, 'STARTED', ?, ?)
        """,
        (aid, attempt_no, row["sha256"] or "", now),
    )
    attempt_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()

    # Manifest-only retry: Telegram already succeeded (file_id present)
    if row["telegram_file_id"]:
        return _finalize_with_manifest(
            conn,
            aid=aid,
            job_id=job_id,
            row=row,
            file_id=row["telegram_file_id"],
            file_unique_id=row["telegram_file_unique_id"],
            message_id=row["telegram_message_id"],
            chat_id=row["telegram_chat_id"] or ch,
            local=local,
        )

    size = int(row["size_bytes"] or 0)
    if size > TELEGRAM_MAX_UPLOAD_BYTES:
        err = f"exceeds Telegram upload limit ({size} > {TELEGRAM_MAX_UPLOAD_BYTES})"
        conn.execute(
            """
            UPDATE archive_upload_attempts SET status='FAILED', error=?, finished_at=?
            WHERE id=?
            """,
            (err, utc_now(), attempt_id),
        )
        conn.execute(
            """
            UPDATE archived_artifacts SET
              status=?, last_error=?, retry_count=max_retries, updated_at=?
            WHERE id=?
            """,
            (STATUS_FAILED, err, utc_now(), aid),
        )
        recompute_package_status(conn, job_id)
        conn.commit()
        return {"artifact_id": aid, "status": STATUS_FAILED, "error": err}

    if local is None or not local.is_file():
        err = "local staging file missing"
        _fail_attempt(conn, attempt_id, aid, job_id, err, int(row["retry_count"]))
        return {"artifact_id": aid, "status": STATUS_FAILED, "error": err}

    # Re-check checksum if file still present
    try:
        digest = sha256_file(local)
    except Exception as e:  # noqa: BLE001
        _fail_attempt(conn, attempt_id, aid, job_id, f"checksum failed: {e}", int(row["retry_count"]))
        return {"artifact_id": aid, "status": STATUS_FAILED, "error": str(e)}

    if row["sha256"] and digest != row["sha256"]:
        conn.execute(
            "UPDATE archived_artifacts SET sha256=?, size_bytes=?, updated_at=? WHERE id=?",
            (digest, local.stat().st_size, utc_now(), aid),
        )
        conn.commit()

    caption = f"{row['artifact_key']}\nsha256={digest[:16]}"
    uploader = upload_fn or default_telegram_upload
    try:
        meta = uploader(local, caption, ch, tok or "")
        file_id = meta.get("file_id")
        if not file_id:
            raise RuntimeError("Telegram response missing file_id")
        conn.execute(
            """
            UPDATE archive_upload_attempts SET
              status='RESPONSE_OK', telegram_file_id=?, telegram_message_id=?, finished_at=?
            WHERE id=?
            """,
            (file_id, meta.get("message_id"), utc_now(), attempt_id),
        )
        # Persist Telegram refs as UPLOADED — INDEXED only after GitHub manifest OK
        now2 = utc_now()
        conn.execute(
            """
            UPDATE archived_artifacts SET
              status = ?,
              telegram_file_id = ?,
              telegram_file_unique_id = COALESCE(?, telegram_file_unique_id),
              telegram_message_id = ?,
              telegram_chat_id = ?,
              uploaded_at = COALESCE(uploaded_at, ?),
              updated_at = ?
            WHERE id = ?
            """,
            (
                STATUS_UPLOADED,
                file_id,
                meta.get("file_unique_id"),
                meta.get("message_id"),
                meta.get("chat_id", ch),
                now2,
                now2,
                aid,
            ),
        )
        conn.commit()
        return _finalize_with_manifest(
            conn,
            aid=aid,
            job_id=job_id,
            row=row,
            file_id=file_id,
            file_unique_id=meta.get("file_unique_id"),
            message_id=meta.get("message_id"),
            chat_id=meta.get("chat_id", ch),
            local=local,
        )
    except Exception as e:  # noqa: BLE001
        _fail_attempt(conn, attempt_id, aid, job_id, str(e), int(row["retry_count"]))
        return {"artifact_id": aid, "status": STATUS_FAILED, "error": str(e)}


def _fail_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    artifact_id: int,
    job_id: int,
    err: str,
    retry_count: int,
) -> None:
    now = utc_now()
    new_retry = retry_count + 1
    row = conn.execute(
        "SELECT max_retries FROM archived_artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    max_r = int(row["max_retries"]) if row else archive_max_retries()
    if new_retry > max_r:
        status = STATUS_FAILED
        next_at = None
    else:
        status = STATUS_PENDING
        next_at = next_attempt_iso(new_retry)
    conn.execute(
        """
        UPDATE archive_upload_attempts SET status='FAILED', error=?, finished_at=?
        WHERE id=?
        """,
        (err, now, attempt_id),
    )
    conn.execute(
        """
        UPDATE archived_artifacts SET
          status=?, retry_count=?, next_attempt_at=?, last_error=?, updated_at=?
        WHERE id=?
        """,
        (status, new_retry, next_at, err, now, artifact_id),
    )
    recompute_package_status(conn, job_id)
    conn.commit()


def _maybe_delete_staging(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
            log.info("deleted staging %s", path)
    except Exception as e:  # noqa: BLE001
        log.warning("staging delete failed %s: %s", path, e)


def get_indexed_artifacts(
    db_path: Path,
    symbol: str,
    day: str,
    kind: str,
    *,
    provider: str = "dukascopy",
) -> list[dict[str, Any]]:
    """Return INDEXED Telegram refs for the requested kind."""
    sym = normalize_symbol(symbol)
    types: list[str] = []
    if kind in ("tick", "both"):
        types.append(ARTIFACT_TICKS)
    if kind in ("1s", "both"):
        types.append(ARTIFACT_BARS_1S)
    # metadata always useful when present
    types.append(ARTIFACT_META)
    conn = connect(db_path)
    out: list[dict[str, Any]] = []
    for at in types:
        ak = artifact_key(provider, sym, day, at)
        row = conn.execute(
            "SELECT * FROM archived_artifacts WHERE artifact_key = ? AND status = ?",
            (ak, STATUS_INDEXED),
        ).fetchone()
        if row and row["telegram_file_id"]:
            out.append(dict(row))
    return out


def package_fully_indexed(
    db_path: Path,
    symbol: str,
    day: str,
    kind: str,
    *,
    provider: str = "dukascopy",
) -> bool:
    """True when all artifacts required for kind are INDEXED."""
    sym = normalize_symbol(symbol)
    need: list[str] = []
    if kind in ("tick", "both"):
        need.append(ARTIFACT_TICKS)
    if kind in ("1s", "both"):
        need.append(ARTIFACT_BARS_1S)
    if not need:
        return False
    conn = connect(db_path)
    for at in need:
        ak = artifact_key(provider, sym, day, at)
        row = conn.execute(
            "SELECT status, telegram_file_id FROM archived_artifacts WHERE artifact_key = ?",
            (ak,),
        ).fetchone()
        if not row or row["status"] != STATUS_INDEXED or not row["telegram_file_id"]:
            return False
    return True


def archive_tick(db_path: Path | None = None, **kwargs: Any) -> dict[str, Any] | None:
    """Single sequential archive step for worker loops."""
    if not archive_enabled() and kwargs.get("upload_fn") is None:
        return None
    path = db_path or sqlite_path()
    recover_stale_uploading(path)
    return process_one_artifact(path, **kwargs)


def reconcile_success_days(db_path: Path, *, provider: str = "dukascopy") -> int:
    """Enqueue archive for SUCCESS download days missing indexed packages."""
    if not archive_enabled():
        return 0
    conn = connect(db_path)
    rows = conn.execute(
        """
        SELECT symbol, day FROM download_days
        WHERE provider = ? AND status IN ('SUCCESS', 'EXPECTED_EMPTY')
        """,
        (provider,),
    ).fetchall()
    n = 0
    for r in rows:
        pk = package_key(provider, r["symbol"], r["day"])
        job = conn.execute(
            "SELECT status FROM archive_jobs WHERE package_key = ?", (pk,)
        ).fetchone()
        if job and job["status"] == STATUS_INDEXED:
            continue
        res = enqueue_archive_for_day(db_path, r["symbol"], r["day"], provider=provider)
        if res.get("job_id"):
            n += 1
    return n
