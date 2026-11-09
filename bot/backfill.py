"""Legacy Telegram archive backfill — register existing channel documents into
the GitHub manifest + SQLite without re-downloading or re-uploading.

Admin forwards documents from the private archive channel to the bot, or uses
/backfill with optional symbol/day context for files that only have a filename.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from bot.archive import (
    ARTIFACT_BARS_1S,
    ARTIFACT_FILE_NAMES,
    ARTIFACT_META,
    ARTIFACT_TICKS,
    STATUS_INDEXED,
    artifact_key,
    package_key,
    recompute_package_status,
)
from bot.config import archive_channel_id, archive_max_retries, manifest_enabled
from db.catalog import connect, utc_now
from downloader.symbols import normalize_symbol

log = logging.getLogger("bot.backfill")

# artifact_key line: dukascopy|USA30IDXUSD|2026-11-07|ticks_parquet
_ARTIFACT_KEY_RE = re.compile(
    r"^(?P<provider>[a-z0-9_]+)\|(?P<symbol>[A-Z0-9]+)\|"
    r"(?P<day>\d{4}-\d{2}-\d{2})\|(?P<atype>[a-z0-9_]+)$",
    re.IGNORECASE,
)

# Older human captions: "US30 | 2026-11-07 | tick" or "USA30IDXUSD 2026-11-07 1s"
_HUMAN_CAPTION_RE = re.compile(
    r"(?P<symbol>[A-Za-z0-9]+)(?:\s*[|]\s*|\s+)"
    r"(?P<day>\d{4}-\d{2}-\d{2})"
    r"(?:\s*[|]\s*|\s+)"
    r"(?P<kind>tick|ticks|1s|bars|ohlc|both|meta|metadata)?",
    re.IGNORECASE,
)

_SHA_RE = re.compile(r"sha256=([0-9a-fA-F]+)")

_FILE_NAME_TO_TYPE = {
    "ticks.parquet": ARTIFACT_TICKS,
    "bars_1s.parquet": ARTIFACT_BARS_1S,
    "metadata.json": ARTIFACT_META,
}

_ATYPE_ALIASES = {
    "ticks_parquet": ARTIFACT_TICKS,
    "tick": ARTIFACT_TICKS,
    "ticks": ARTIFACT_TICKS,
    "raw": ARTIFACT_TICKS,
    "bars_1s_parquet": ARTIFACT_BARS_1S,
    "1s": ARTIFACT_BARS_1S,
    "bars": ARTIFACT_BARS_1S,
    "ohlc": ARTIFACT_BARS_1S,
    "bar": ARTIFACT_BARS_1S,
    "metadata_json": ARTIFACT_META,
    "meta": ARTIFACT_META,
    "metadata": ARTIFACT_META,
}


@dataclass
class ParsedArchiveDoc:
    provider: str
    symbol: str
    day: str
    artifact_type: str
    file_name: str
    file_id: str
    file_unique_id: str | None
    message_id: int | None
    chat_id: int | None
    size_bytes: int | None
    sha256: str
    source_caption: str


class BackfillError(ValueError):
    """User-facing parse / validation error."""


def _normalize_atype(raw: str | None) -> str | None:
    if not raw:
        return None
    return _ATYPE_ALIASES.get(raw.strip().lower())


def parse_archive_document(
    *,
    file_id: str,
    file_name: str | None = None,
    caption: str | None = None,
    file_unique_id: str | None = None,
    message_id: int | None = None,
    chat_id: int | None = None,
    file_size: int | None = None,
    hint_symbol: str | None = None,
    hint_day: str | None = None,
    hint_provider: str = "dukascopy",
) -> ParsedArchiveDoc:
    """Identify symbol/day/type from caption, filename, and optional admin hints."""
    if not file_id or not str(file_id).strip():
        raise BackfillError("Document has no file_id.")

    caption = (caption or "").strip()
    file_name = (file_name or "").strip() or None
    provider = hint_provider
    symbol: str | None = None
    day: str | None = None
    atype: str | None = None
    sha256 = ""

    # 1) Primary line of caption is often the artifact_key
    first_line = caption.split("\n", 1)[0].strip() if caption else ""
    m = _ARTIFACT_KEY_RE.match(first_line)
    if m:
        provider = m.group("provider").lower()
        symbol = normalize_symbol(m.group("symbol"))
        day = m.group("day")
        atype = _normalize_atype(m.group("atype"))
    else:
        hm = _HUMAN_CAPTION_RE.search(caption.replace("  ", " ")) if caption else None
        if hm:
            symbol = normalize_symbol(hm.group("symbol"))
            day = hm.group("day")
            atype = _normalize_atype(hm.group("kind") or "")

    # sha256 from caption if present
    sm = _SHA_RE.search(caption) if caption else None
    if sm:
        sha256 = sm.group(1).lower()

    # 2) Filename → artifact type
    if file_name:
        base = Path(file_name).name.lower()
        if base in _FILE_NAME_TO_TYPE:
            atype = atype or _FILE_NAME_TO_TYPE[base]

    # 3) Admin hints fill gaps
    if hint_symbol:
        symbol = normalize_symbol(hint_symbol)
    if hint_day:
        try:
            day = date.fromisoformat(hint_day.strip()).isoformat()
        except ValueError as e:
            raise BackfillError(f"Invalid hint day '{hint_day}'. Use YYYY-MM-DD.") from e

    if not symbol:
        raise BackfillError(
            "Could not determine symbol. Forward a document with a full caption "
            "or use /backfill SYMBOL YYYY-MM-DD then forward the files."
        )
    if not day:
        raise BackfillError(
            "Could not determine day. Caption should include YYYY-MM-DD "
            "or set context with /backfill SYMBOL YYYY-MM-DD."
        )
    try:
        date.fromisoformat(day)
    except ValueError as e:
        raise BackfillError(f"Invalid day '{day}'.") from e

    if not atype:
        raise BackfillError(
            f"Could not determine artifact type from caption/filename '{file_name}'. "
            "Expected ticks.parquet, bars_1s.parquet, or metadata.json."
        )

    resolved_name = ARTIFACT_FILE_NAMES.get(atype, file_name or "artifact.bin")
    return ParsedArchiveDoc(
        provider=provider,
        symbol=symbol,
        day=day,
        artifact_type=atype,
        file_name=resolved_name,
        file_id=str(file_id).strip(),
        file_unique_id=file_unique_id,
        message_id=message_id,
        chat_id=chat_id,
        size_bytes=file_size,
        sha256=sha256,
        source_caption=caption,
    )


def register_legacy_artifact(
    db_path: Path,
    parsed: ParsedArchiveDoc,
    *,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """Index one existing Telegram document into SQLite + optional GitHub manifest.

    Never uploads or downloads the binary. Idempotent for the same artifact_key
    with the same file_id; refuses to overwrite a different valid INDEXED file_id.
    """
    from bot.manifest import upsert_artifact_remote

    pk = package_key(parsed.provider, parsed.symbol, parsed.day)
    ak = artifact_key(parsed.provider, parsed.symbol, parsed.day, parsed.artifact_type)
    now = utc_now()
    max_r = archive_max_retries()
    conn = connect(db_path)

    # Ensure archive_jobs row
    job = conn.execute(
        "SELECT * FROM archive_jobs WHERE package_key = ?", (pk,)
    ).fetchone()
    if job:
        job_id = int(job["id"])
    else:
        cur = conn.execute(
            """
            INSERT INTO archive_jobs (
              package_key, symbol, provider, day, status,
              artifact_expected, artifact_indexed, retry_count, max_retries,
              created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
            """,
            (
                pk,
                parsed.symbol,
                parsed.provider,
                parsed.day,
                STATUS_INDEXED,
                max_r,
                now,
                now,
            ),
        )
        job_id = int(cur.lastrowid)

    existing = conn.execute(
        "SELECT * FROM archived_artifacts WHERE artifact_key = ?", (ak,)
    ).fetchone()

    if existing and existing["status"] == STATUS_INDEXED and existing["telegram_file_id"]:
        if existing["telegram_file_id"] == parsed.file_id:
            return {
                "ok": True,
                "action": "already_indexed",
                "artifact_key": ak,
                "package_key": pk,
                "file_id": parsed.file_id,
                "manifest_written": False,
            }
        return {
            "ok": False,
            "action": "conflict",
            "error": (
                f"Artifact {ak} already INDEXED with a different file_id. "
                "Refusing to overwrite."
            ),
            "artifact_key": ak,
            "existing_file_id": existing["telegram_file_id"],
        }

    manifest_ok = False
    if write_manifest and manifest_enabled():
        artifact_payload = {
            "file_id": parsed.file_id,
            "file_unique_id": parsed.file_unique_id,
            "message_id": parsed.message_id,
            "chat_id": str(parsed.chat_id) if parsed.chat_id is not None else None,
            "sha256": parsed.sha256 or (existing["sha256"] if existing else "") or "",
            "size_bytes": parsed.size_bytes
            if parsed.size_bytes is not None
            else (existing["size_bytes"] if existing else None),
            "status": STATUS_INDEXED,
            "artifact_type": parsed.artifact_type,
        }
        try:
            manifest_ok = bool(
                upsert_artifact_remote(
                    package_key=pk,
                    provider=parsed.provider,
                    symbol=parsed.symbol,
                    day=parsed.day,
                    file_name=parsed.file_name,
                    artifact=artifact_payload,
                )
            )
        except Exception as e:  # noqa: BLE001
            log.exception("manifest write during backfill failed")
            return {
                "ok": False,
                "action": "manifest_failed",
                "error": str(e),
                "artifact_key": ak,
            }
    elif not manifest_enabled():
        # Still allow SQLite-only backfill when manifest is off
        manifest_ok = False

    sha = parsed.sha256 or (existing["sha256"] if existing else "") or ""
    size = parsed.size_bytes if parsed.size_bytes is not None else (
        existing["size_bytes"] if existing else None
    )

    if existing:
        conn.execute(
            """
            UPDATE archived_artifacts SET
              status = ?,
              telegram_file_id = ?,
              telegram_file_unique_id = COALESCE(?, telegram_file_unique_id),
              telegram_message_id = COALESCE(?, telegram_message_id),
              telegram_chat_id = COALESCE(?, telegram_chat_id),
              file_name = ?,
              size_bytes = COALESCE(?, size_bytes),
              sha256 = CASE WHEN ? != '' THEN ? ELSE sha256 END,
              uploaded_at = COALESCE(uploaded_at, ?),
              indexed_at = ?,
              last_error = NULL,
              updated_at = ?
            WHERE id = ?
            """,
            (
                STATUS_INDEXED,
                parsed.file_id,
                parsed.file_unique_id,
                parsed.message_id,
                parsed.chat_id,
                parsed.file_name,
                size,
                sha,
                sha,
                now,
                now,
                now,
                int(existing["id"]),
            ),
        )
        action = "updated"
    else:
        conn.execute(
            """
            INSERT INTO archived_artifacts (
              archive_job_id, artifact_key, symbol, provider, day, artifact_type,
              file_name, local_path, size_bytes, sha256, status,
              retry_count, max_retries,
              telegram_file_id, telegram_file_unique_id, telegram_message_id, telegram_chat_id,
              uploaded_at, indexed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                ak,
                parsed.symbol,
                parsed.provider,
                parsed.day,
                parsed.artifact_type,
                parsed.file_name,
                size,
                sha,
                STATUS_INDEXED,
                max_r,
                parsed.file_id,
                parsed.file_unique_id,
                parsed.message_id,
                parsed.chat_id,
                now,
                now,
                now,
                now,
            ),
        )
        action = "inserted"

    cnt = conn.execute(
        "SELECT COUNT(*) AS c FROM archived_artifacts WHERE archive_job_id = ?",
        (job_id,),
    ).fetchone()["c"]
    conn.execute(
        "UPDATE archive_jobs SET artifact_expected = ?, updated_at = ? WHERE id = ?",
        (int(cnt), now, job_id),
    )
    recompute_package_status(conn, job_id)
    conn.commit()

    return {
        "ok": True,
        "action": action,
        "artifact_key": ak,
        "package_key": pk,
        "file_id": parsed.file_id,
        "symbol": parsed.symbol,
        "day": parsed.day,
        "artifact_type": parsed.artifact_type,
        "file_name": parsed.file_name,
        "manifest_written": manifest_ok if manifest_enabled() else False,
        "manifest_enabled": manifest_enabled(),
    }


def backfill_context_summary(user_data: dict) -> str:
    sym = user_data.get("backfill_symbol")
    day = user_data.get("backfill_day")
    if sym and day:
        return f"Context: {sym} {day}. Forward archive documents now."
    return (
        "No symbol/day context set. Forward documents that have full captions, "
        "or /backfill SYMBOL YYYY-MM-DD first."
    )
