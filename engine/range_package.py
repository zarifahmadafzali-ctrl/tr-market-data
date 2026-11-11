"""Ephemeral range ZIP delivery packages.

Daily Telegram archive + GitHub manifest remain the durable source of truth.
ZIP files are built on demand for a user range, sent to chat, then discarded
(not treated as the archive of record).
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

log = logging.getLogger("engine.range_package")

# Stay under Telegram Bot API ~50MB document limit
DEFAULT_PART_MAX_BYTES = 45 * 1024 * 1024
ARTIFACT_ORDER = ("ticks.parquet", "bars_1s.parquet", "metadata.json")


@dataclass
class DayFiles:
    day: str
    files: dict[str, Path] = field(default_factory=dict)  # name -> local path
    source: str = "local"  # local | telegram | mixed
    errors: list[str] = field(default_factory=list)


@dataclass
class PackagePlan:
    symbol: str
    start: str
    end: str
    kind: str
    included_days: list[str]
    empty_days: list[str]
    failed_days: list[str]
    skipped_days: list[str]
    day_files: list[DayFiles]
    notes: list[str] = field(default_factory=list)


def _parse_day(s: str) -> date:
    return date.fromisoformat(s[:10])


def daterange_iso(start: str, end: str) -> list[str]:
    a, b = _parse_day(start), _parse_day(end)
    out: list[str] = []
    d = a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def kind_file_names(kind: str) -> list[str]:
    k = (kind or "both").lower()
    if k in ("tick", "ticks"):
        return ["ticks.parquet", "metadata.json"]
    if k in ("1s", "bars"):
        return ["bars_1s.parquet", "metadata.json"]
    return list(ARTIFACT_ORDER)


def local_day_paths(
    data_root: Path,
    symbol: str,
    day_s: str,
    *,
    provider: str = "dukascopy",
) -> dict[str, Path]:
    from storage.paths import (
        bars_1s_parquet_path,
        day_dir,
        metadata_json_path,
        ticks_parquet_path,
    )

    d = _parse_day(day_s)
    base = day_dir(data_root, provider=provider, symbol=symbol, day=d)
    return {
        "ticks.parquet": ticks_parquet_path(base),
        "bars_1s.parquet": bars_1s_parquet_path(base),
        "metadata.json": metadata_json_path(base),
    }


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def telegram_get_file_path(token: str, file_id: str, timeout: int = 60) -> str | None:
    """Return Telegram file_path for getFile, or None."""
    if not token or not file_id:
        return None
    url = f"https://api.telegram.org/bot{token}/getFile?{urlencode({'file_id': file_id})}"
    try:
        with urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.loads(resp.read().decode() or "{}")
        if not data.get("ok"):
            return None
        return (data.get("result") or {}).get("file_path")
    except (HTTPError, URLError, TimeoutError, ValueError) as e:
        log.warning("getFile failed: %s", e)
        return None


def telegram_download_file(
    token: str,
    file_path: str,
    dest: Path,
    timeout: int = 300,
) -> bool:
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urlopen(url, timeout=timeout) as resp, dest.open("wb") as out:  # noqa: S310
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return dest.is_file() and dest.stat().st_size > 0
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        log.warning("download file failed %s: %s", file_path, e)
        return False


def file_ids_from_manifest(
    manifest: dict[str, Any] | None,
    symbol: str,
    day_s: str,
    names: list[str],
    *,
    provider: str = "dukascopy",
) -> dict[str, str]:
    if not manifest:
        return {}
    pk = f"{provider}|{symbol}|{day_s}"
    pkg = (manifest.get("packages") or {}).get(pk) or {}
    arts = pkg.get("artifacts") or {}
    out: dict[str, str] = {}
    for name in names:
        art = arts.get(name) or {}
        fid = art.get("file_id") or art.get("telegram_file_id")
        st = art.get("status")
        if fid and (not st or st in ("INDEXED", "UPLOADED")):
            out[name] = str(fid)
    return out


def file_ids_from_sqlite(
    db_path: Path,
    symbol: str,
    day_s: str,
    names: list[str],
    *,
    provider: str = "dukascopy",
) -> dict[str, str]:
    try:
        from bot.archive import ARTIFACT_FILE_NAMES, artifact_key, connect, STATUS_INDEXED
        from downloader.symbols import normalize_symbol
    except Exception:
        return {}
    # reverse name -> type
    name_to_type = {v: k for k, v in ARTIFACT_FILE_NAMES.items()} if hasattr(
        __import__("bot.archive", fromlist=["ARTIFACT_FILE_NAMES"]), "ARTIFACT_FILE_NAMES"
    ) else {}
    try:
        from bot.archive import ARTIFACT_BARS_1S, ARTIFACT_META, ARTIFACT_TICKS

        name_to_type = {
            "ticks.parquet": ARTIFACT_TICKS,
            "bars_1s.parquet": ARTIFACT_BARS_1S,
            "metadata.json": ARTIFACT_META,
        }
    except Exception:
        name_to_type = {
            "ticks.parquet": "ticks",
            "bars_1s.parquet": "bars_1s",
            "metadata.json": "metadata",
        }

    sym = normalize_symbol(symbol)
    out: dict[str, str] = {}
    try:
        conn = connect(db_path)
        for name in names:
            at = name_to_type.get(name)
            if not at:
                continue
            ak = artifact_key(provider, sym, day_s, at)
            row = conn.execute(
                "SELECT status, telegram_file_id FROM archived_artifacts WHERE artifact_key = ?",
                (ak,),
            ).fetchone()
            if row and row["status"] == STATUS_INDEXED and row["telegram_file_id"]:
                out[name] = str(row["telegram_file_id"])
    except Exception as e:  # noqa: BLE001
        log.warning("sqlite file_id lookup failed: %s", e)
    return out


def resolve_day_files(
    *,
    symbol: str,
    day_s: str,
    kind: str,
    data_root: Path,
    work_dir: Path,
    token: str,
    db_path: Path | None,
    manifest: dict[str, Any] | None,
    provider: str = "dukascopy",
) -> DayFiles:
    names = kind_file_names(kind)
    local = local_day_paths(data_root, symbol, day_s, provider=provider)
    df = DayFiles(day=day_s)
    sources: set[str] = set()

    ids = file_ids_from_sqlite(db_path, symbol, day_s, names, provider=provider) if db_path else {}
    if not ids:
        ids = file_ids_from_manifest(manifest, symbol, day_s, names, provider=provider)

    for name in names:
        lp = local.get(name)
        if lp is not None and lp.is_file() and lp.stat().st_size > 0:
            df.files[name] = lp
            sources.add("local")
            continue
        fid = ids.get(name)
        if fid and token:
            dest = work_dir / day_s / name
            fpath = telegram_get_file_path(token, fid)
            if fpath and telegram_download_file(token, fpath, dest):
                df.files[name] = dest
                sources.add("telegram")
            else:
                df.errors.append(f"{name}: getFile failed")
        else:
            df.errors.append(f"{name}: not local and no file_id")

    if sources == {"local"}:
        df.source = "local"
    elif sources == {"telegram"}:
        df.source = "telegram"
    elif sources:
        df.source = "mixed"
    return df


def build_package_json(plan: PackagePlan, part_index: int, part_count: int, files_in_part: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "package_type": "range_delivery",
        "ephemeral": True,
        "symbol": plan.symbol,
        "start": plan.start,
        "end": plan.end,
        "kind": plan.kind,
        "included_days": plan.included_days,
        "empty_days": plan.empty_days,
        "failed_days": plan.failed_days,
        "skipped_days": plan.skipped_days,
        "part_index": part_index,
        "part_count": part_count,
        "files": files_in_part,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "notes": plan.notes,
    }


def assign_parts(
    day_files: list[DayFiles],
    symbol: str,
    max_bytes: int = DEFAULT_PART_MAX_BYTES,
) -> list[list[tuple[str, Path]]]:
    """Greedy pack (arcname, path) into parts under max_bytes (excluding package.json ~4KB)."""
    margin = min(64 * 1024, max(0, max_bytes // 20))
    budget = max(1024, max_bytes - margin)
    parts: list[list[tuple[str, Path]]] = []
    current: list[tuple[str, Path]] = []
    size = 0

    def flush() -> None:
        nonlocal current, size
        if current:
            parts.append(current)
            current = []
            size = 0

    for df in day_files:
        for name, path in sorted(df.files.items()):
            try:
                sz = path.stat().st_size
            except OSError:
                continue
            arc = f"{symbol}/{df.day}/{name}"
            if current and size + sz > budget:
                flush()
            current.append((arc, path))
            size += sz
            # single file larger than budget still goes alone
            if size >= budget:
                flush()
    flush()
    return parts or [[]]


def write_zip_part(
    part_entries: list[tuple[str, Path]],
    package_obj: dict[str, Any],
    dest: Path,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("package.json", json.dumps(package_obj, indent=2, sort_keys=True))
        for arc, path in part_entries:
            zf.write(path, arcname=arc)
    return dest


def collect_plan(
    *,
    symbol: str,
    start: str,
    end: str,
    kind: str,
    progress: dict[str, Any],
    data_root: Path,
    work_dir: Path,
    token: str,
    db_path: Path | None,
    manifest: dict[str, Any] | None,
    provider: str = "dukascopy",
) -> PackagePlan:
    done = set(progress.get("done_days") or [])
    empty = set(progress.get("empty_days") or [])
    failed = set(progress.get("failed_days") or [])
    skipped = set(progress.get("skipped_days") or [])
    # skipped INDEXED days are deliverable
    candidate = sorted(done | skipped)
    day_files: list[DayFiles] = []
    included: list[str] = []
    notes: list[str] = []

    for day_s in candidate:
        if day_s in empty:
            continue
        df = resolve_day_files(
            symbol=symbol,
            day_s=day_s,
            kind=kind,
            data_root=data_root,
            work_dir=work_dir,
            token=token,
            db_path=db_path,
            manifest=manifest,
            provider=provider,
        )
        if df.files:
            day_files.append(df)
            included.append(day_s)
        else:
            notes.append(f"{day_s}: no files resolved ({'; '.join(df.errors) or 'unknown'})")
            if day_s not in failed:
                failed.add(day_s)

    return PackagePlan(
        symbol=symbol,
        start=start,
        end=end,
        kind=kind,
        included_days=included,
        empty_days=sorted(empty),
        failed_days=sorted(failed),
        skipped_days=sorted(skipped),
        day_files=day_files,
        notes=notes,
    )


def build_range_zip_parts(
    plan: PackagePlan,
    out_dir: Path,
    *,
    max_bytes: int = DEFAULT_PART_MAX_BYTES,
) -> list[Path]:
    if not plan.day_files:
        return []
    parts_entries = assign_parts(plan.day_files, plan.symbol, max_bytes=max_bytes)
    # filter empty
    parts_entries = [p for p in parts_entries if p]
    if not parts_entries:
        return []
    n = len(parts_entries)
    paths: list[Path] = []
    for i, entries in enumerate(parts_entries, start=1):
        files_meta = []
        for arc, path in entries:
            try:
                files_meta.append(
                    {
                        "path": arc,
                        "size_bytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
            except OSError:
                files_meta.append({"path": arc, "size_bytes": 0})
        pkg = build_package_json(plan, i, n, files_meta)
        fname = f"{plan.symbol}_{plan.start}_{plan.end}_part{i}of{n}.zip"
        dest = out_dir / fname
        write_zip_part(entries, pkg, dest)
        paths.append(dest)
        log.info("range zip part %s/%s %s bytes=%s", i, n, dest.name, dest.stat().st_size)
    return paths


def deliver_range_package(
    symbol: str,
    start: str,
    end: str,
    kind: str,
    progress: dict[str, Any],
    *,
    data_root: Path,
    db_path: Path,
    token: str,
    chat_id: int | None,
    manifest: dict[str, Any] | None = None,
    provider: str = "dukascopy",
    max_bytes: int = DEFAULT_PART_MAX_BYTES,
    send_document_path: Callable[[int, Path, str], bool] | None = None,
    send_message: Callable[[int, str], bool] | None = None,
) -> dict[str, Any]:
    """Build ephemeral ZIP part(s) and send to chat. Returns summary dict."""
    if chat_id is None or not token:
        return {"status": "skipped", "reason": "no chat_id or token"}

    with tempfile.TemporaryDirectory(prefix="tr-range-") as tmp:
        work = Path(tmp)
        plan = collect_plan(
            symbol=symbol,
            start=start,
            end=end,
            kind=kind,
            progress=progress,
            data_root=data_root,
            work_dir=work / "dl",
            token=token,
            db_path=db_path,
            manifest=manifest,
            provider=provider,
        )
        if not plan.included_days:
            msg = (
                f"No files to package for {symbol} {start}→{end}.\n"
                f"empty={len(plan.empty_days)} failed={len(plan.failed_days)}"
            )
            if send_message:
                send_message(chat_id, msg)
            return {"status": "empty", "plan": _plan_public(plan)}

        zips = build_range_zip_parts(plan, work / "out", max_bytes=max_bytes)
        if not zips:
            if send_message:
                send_message(chat_id, f"ZIP build failed for {symbol} {start}→{end}")
            return {"status": "build_failed", "plan": _plan_public(plan)}

        coverage = "complete" if not plan.failed_days else "partial"
        header = (
            f"Range package {coverage}: {symbol} {start}→{end}\n"
            f"included={len(plan.included_days)} empty={len(plan.empty_days)} "
            f"failed={len(plan.failed_days)} parts={len(zips)}"
        )
        if plan.failed_days:
            header += f"\nFailed/missing: {', '.join(plan.failed_days[:15])}"
            if len(plan.failed_days) > 15:
                header += "…"
        if send_message:
            send_message(chat_id, header)

        sent = 0
        for i, zp in enumerate(zips, start=1):
            cap = f"{symbol} {start}→{end} · part {i}/{len(zips)} ({coverage})"
            ok = False
            if send_document_path:
                ok = send_document_path(chat_id, zp, cap)
            if ok:
                sent += 1
            else:
                log.warning("failed to send zip part %s", zp.name)

        return {
            "status": "ok" if sent == len(zips) else "partial_send",
            "parts": len(zips),
            "sent": sent,
            "coverage": coverage,
            "plan": _plan_public(plan),
        }


def _plan_public(plan: PackagePlan) -> dict[str, Any]:
    return {
        "symbol": plan.symbol,
        "start": plan.start,
        "end": plan.end,
        "kind": plan.kind,
        "included_days": plan.included_days,
        "empty_days": plan.empty_days,
        "failed_days": plan.failed_days,
        "skipped_days": plan.skipped_days,
        "notes": plan.notes,
    }
