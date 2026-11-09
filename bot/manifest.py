"""Durable GitHub JSON archive manifest — index only, not file storage.

Telegram holds blobs (file_id). GitHub holds the durable package index so
ephemeral Render SQLite can be rebuilt after restart without re-download.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

import httpx

from bot.config import (
    manifest_branch,
    manifest_enabled,
    manifest_path,
    manifest_repo,
    manifest_token,
)

log = logging.getLogger("bot.manifest")

SCHEMA_VERSION = 1
_EMPTY: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "updated_at": "",
    "packages": {},
}

# Serialize manifest read-modify-write across worker threads in one process
_manifest_lock = threading.Lock()

# Injectables for tests
FetchFn = Any
PutFn = Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_manifest() -> dict[str, Any]:
    m = deepcopy(_EMPTY)
    m["updated_at"] = _utc_now()
    return m


def validate_manifest(data: Any) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "manifest root must be object"
    ver = data.get("schema_version")
    if ver is None:
        return False, "missing schema_version"
    if not isinstance(ver, int) or ver > SCHEMA_VERSION:
        return False, f"unsupported schema_version={ver}"
    if ver < 1:
        return False, f"invalid schema_version={ver}"
    packages = data.get("packages")
    if packages is None:
        data["packages"] = {}
        packages = data["packages"]
    if not isinstance(packages, dict):
        return False, "packages must be object"
    for pk, pkg in packages.items():
        if not isinstance(pkg, dict):
            return False, f"package {pk} must be object"
        arts = pkg.get("artifacts")
        if arts is None:
            pkg["artifacts"] = {}
            continue
        if not isinstance(arts, dict):
            return False, f"package {pk} artifacts must be object"
        for name, art in arts.items():
            if not isinstance(art, dict):
                return False, f"artifact {pk}/{name} must be object"
            if art.get("status") == "INDEXED" and not art.get("file_id"):
                return False, f"INDEXED artifact {pk}/{name} missing file_id"
    return True, "ok"


def merge_artifact_into_manifest(
    manifest: dict[str, Any],
    *,
    package_key: str,
    provider: str,
    symbol: str,
    day: str,
    file_name: str,
    artifact: dict[str, Any],
) -> dict[str, Any]:
    """Return new manifest with one artifact merged; never deletes other entries."""
    out = deepcopy(manifest) if manifest else empty_manifest()
    if "packages" not in out or not isinstance(out["packages"], dict):
        out["packages"] = {}
    pkg = out["packages"].get(package_key)
    if not isinstance(pkg, dict):
        pkg = {
            "provider": provider,
            "symbol": symbol,
            "day": day,
            "artifacts": {},
        }
        out["packages"][package_key] = pkg
    else:
        pkg.setdefault("provider", provider)
        pkg.setdefault("symbol", symbol)
        pkg.setdefault("day", day)
        if "artifacts" not in pkg or not isinstance(pkg["artifacts"], dict):
            pkg["artifacts"] = {}
    # Preserve existing artifact fields when merging partial updates
    prev = pkg["artifacts"].get(file_name)
    if isinstance(prev, dict):
        merged = {**prev, **{k: v for k, v in artifact.items() if v is not None}}
    else:
        merged = dict(artifact)
    pkg["artifacts"][file_name] = merged
    out["schema_version"] = SCHEMA_VERSION
    out["updated_at"] = _utc_now()
    return out


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tr-market-data-archive-manifest",
    }


def _contents_url(repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{repo}/contents/{path.lstrip('/')}"


def fetch_manifest(
    *,
    token: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Fetch manifest. Returns (manifest_or_None, github_blob_sha_or_None, status).

    status: ok | missing | error | disabled | misconfigured
    """
    if not manifest_enabled() and token is None:
        # allow tests to pass token even if env disabled
        if not (token or manifest_token()):
            return None, None, "disabled"

    tok = token if token is not None else manifest_token()
    r = repo if repo is not None else manifest_repo()
    p = path if path is not None else manifest_path()
    b = branch if branch is not None else manifest_branch()

    if not tok or not r:
        return None, None, "misconfigured"

    url = _contents_url(r, p)
    own = client is None
    cli = client or httpx.Client(timeout=30.0)
    try:
        resp = cli.get(url, headers=_api_headers(tok), params={"ref": b})
        if resp.status_code == 404:
            return empty_manifest(), None, "missing"
        if resp.status_code in (401, 403):
            log.warning("GitHub manifest fetch unauthorized (check token scopes / private repo)")
            return None, None, "error"
        if resp.status_code != 200:
            log.warning("GitHub manifest fetch HTTP %s", resp.status_code)
            return None, None, "error"
        payload = resp.json()
        gh_sha = payload.get("sha")
        content_b64 = payload.get("content") or ""
        if payload.get("encoding") == "base64":
            raw = base64.b64decode(content_b64.replace("\n", ""))
        else:
            raw = content_b64.encode("utf-8") if isinstance(content_b64, str) else content_b64
        data = json.loads(raw.decode("utf-8"))
        ok, reason = validate_manifest(data)
        if not ok:
            log.error("GitHub manifest invalid: %s — leaving local SQLite unchanged", reason)
            return None, None, "error"
        return data, gh_sha, "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("GitHub manifest fetch failed: %s", e)
        return None, None, "error"
    finally:
        if own:
            cli.close()


def put_manifest(
    manifest: dict[str, Any],
    *,
    expected_sha: str | None,
    token: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    client: httpx.Client | None = None,
    message: str = "update archive manifest",
) -> tuple[bool, str | None, str]:
    """Write manifest. Returns (ok, new_sha_or_None, status: ok|conflict|error|misconfigured)."""
    tok = token if token is not None else manifest_token()
    r = repo if repo is not None else manifest_repo()
    p = path if path is not None else manifest_path()
    b = branch if branch is not None else manifest_branch()
    if not tok or not r:
        return False, None, "misconfigured"

    ok, reason = validate_manifest(manifest)
    if not ok:
        log.error("Refusing to push invalid manifest: %s", reason)
        return False, None, "error"

    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        ).decode("ascii"),
        "branch": b,
    }
    if expected_sha:
        body["sha"] = expected_sha

    url = _contents_url(r, p)
    own = client is None
    cli = client or httpx.Client(timeout=30.0)
    try:
        resp = cli.put(url, headers=_api_headers(tok), json=body)
        if resp.status_code in (200, 201):
            new_sha = (resp.json().get("content") or {}).get("sha")
            return True, new_sha, "ok"
        if resp.status_code == 409:
            return False, None, "conflict"
        if resp.status_code == 422 and expected_sha:
            # stale sha often surfaces as 422
            return False, None, "conflict"
        log.warning("GitHub manifest put HTTP %s", resp.status_code)
        return False, None, "error"
    except Exception as e:  # noqa: BLE001
        log.warning("GitHub manifest put failed: %s", e)
        return False, None, "error"
    finally:
        if own:
            cli.close()


def upsert_artifact_remote(
    *,
    package_key: str,
    provider: str,
    symbol: str,
    day: str,
    file_name: str,
    artifact: dict[str, Any],
    max_conflict_retries: int = 5,
    token: str | None = None,
    repo: str | None = None,
    path: str | None = None,
    branch: str | None = None,
    fetch_fn: Any = None,
    put_fn: Any = None,
) -> bool:
    """Atomically merge one artifact into the remote manifest (handles 409)."""
    if not manifest_enabled() and token is None and fetch_fn is None:
        return True  # manifest disabled — treat as durable enough for local INDEXED

    fetch = fetch_fn or fetch_manifest
    put = put_fn or put_manifest

    with _manifest_lock:
        for attempt in range(max_conflict_retries + 1):
            current, sha, st = fetch(
                token=token, repo=repo, path=path, branch=branch
            )
            if st in ("error", "misconfigured"):
                return False
            if st == "disabled":
                return True
            if current is None:
                current = empty_manifest()
            merged = merge_artifact_into_manifest(
                current,
                package_key=package_key,
                provider=provider,
                symbol=symbol,
                day=day,
                file_name=file_name,
                artifact=artifact,
            )
            ok, _new_sha, pst = put(
                merged,
                expected_sha=sha,
                token=token,
                repo=repo,
                path=path,
                branch=branch,
                message=f"archive: {package_key} / {file_name}",
            )
            if ok:
                return True
            if pst == "conflict":
                log.info("GitHub manifest conflict — refetch merge retry %s", attempt + 1)
                continue
            return False
        return False


def lookup_package(
    manifest: dict[str, Any],
    package_key: str,
) -> dict[str, Any] | None:
    packages = manifest.get("packages") or {}
    pkg = packages.get(package_key)
    return pkg if isinstance(pkg, dict) else None


def lookup_artifact(
    manifest: dict[str, Any],
    package_key: str,
    file_name: str,
) -> dict[str, Any] | None:
    pkg = lookup_package(manifest, package_key)
    if not pkg:
        return None
    arts = pkg.get("artifacts") or {}
    art = arts.get(file_name)
    if isinstance(art, dict) and art.get("status") == "INDEXED" and art.get("file_id"):
        return art
    return None


def rebuild_sqlite_from_manifest(
    db_path: Any,
    manifest: dict[str, Any],
    *,
    connect_fn: Any = None,
) -> dict[str, int]:
    """Reconcile SQLite archive tables from durable manifest INDEXED entries.

    Never downgrades local PENDING/UPLOADING/UPLOADED work-in-progress.
    Idempotent.
    """
    from pathlib import Path

    from bot.archive import (
        ARTIFACT_FILE_NAMES,
        STATUS_INDEXED,
        STATUS_PENDING,
        STATUS_UPLOADING,
        STATUS_UPLOADED,
        artifact_key,
        package_key as make_pk,
        recompute_package_status,
    )
    from bot.config import archive_max_retries
    from db.catalog import connect, utc_now

    # reverse map file_name -> artifact_type
    name_to_type = {v: k for k, v in ARTIFACT_FILE_NAMES.items()}

    path = Path(db_path) if not isinstance(db_path, Path) else db_path
    conn = (connect_fn or connect)(path)
    now = utc_now()
    max_r = archive_max_retries()
    stats = {"packages": 0, "artifacts_upserted": 0, "skipped_local_wip": 0}

    packages = manifest.get("packages") or {}
    if not isinstance(packages, dict):
        return stats

    for pk, pkg in packages.items():
        if not isinstance(pkg, dict):
            continue
        provider = str(pkg.get("provider") or "dukascopy")
        symbol = str(pkg.get("symbol") or "")
        day = str(pkg.get("day") or "")
        if not symbol or not day:
            # try parse package_key
            parts = str(pk).split("|")
            if len(parts) >= 3:
                provider, symbol, day = parts[0], parts[1], parts[2]
            else:
                continue
        arts = pkg.get("artifacts") or {}
        if not isinstance(arts, dict):
            continue

        # ensure archive_jobs row
        job = conn.execute(
            "SELECT id, status FROM archive_jobs WHERE package_key = ?", (pk,)
        ).fetchone()
        if job is None:
            conn.execute(
                """
                INSERT INTO archive_jobs (
                  package_key, symbol, provider, day, status,
                  artifact_expected, artifact_indexed, retry_count, max_retries,
                  created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?, ?, ?)
                """,
                (pk, symbol, provider, day, STATUS_PENDING, max_r, now, now),
            )
            job_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            stats["packages"] += 1
        else:
            job_id = int(job["id"])

        for file_name, art in arts.items():
            if not isinstance(art, dict):
                continue
            if art.get("status") != "INDEXED" or not art.get("file_id"):
                continue
            atype = name_to_type.get(file_name)
            if not atype:
                # allow raw artifact_type keys as fallback
                atype = str(art.get("artifact_type") or file_name)
            ak = artifact_key(provider, symbol, day, atype)
            existing = conn.execute(
                "SELECT id, status, telegram_file_id FROM archived_artifacts WHERE artifact_key = ?",
                (ak,),
            ).fetchone()
            if existing:
                st = existing["status"]
                # Do not overwrite in-flight local work with older manifest
                if st in (STATUS_PENDING, STATUS_UPLOADING, STATUS_UPLOADED) and not existing["telegram_file_id"]:
                    stats["skipped_local_wip"] += 1
                    continue
                if st == STATUS_INDEXED and existing["telegram_file_id"]:
                    continue  # already good
                conn.execute(
                    """
                    UPDATE archived_artifacts SET
                      status = ?,
                      telegram_file_id = ?,
                      telegram_file_unique_id = COALESCE(?, telegram_file_unique_id),
                      telegram_message_id = COALESCE(?, telegram_message_id),
                      telegram_chat_id = COALESCE(?, telegram_chat_id),
                      sha256 = COALESCE(?, sha256),
                      size_bytes = COALESCE(?, size_bytes),
                      file_name = ?,
                      indexed_at = COALESCE(indexed_at, ?),
                      updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        STATUS_INDEXED,
                        art.get("file_id"),
                        art.get("file_unique_id"),
                        art.get("message_id"),
                        art.get("chat_id"),
                        art.get("sha256"),
                        art.get("size_bytes"),
                        file_name,
                        now,
                        now,
                        int(existing["id"]),
                    ),
                )
                stats["artifacts_upserted"] += 1
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
                        symbol,
                        provider,
                        day,
                        atype,
                        file_name,
                        art.get("size_bytes"),
                        art.get("sha256") or "",
                        STATUS_INDEXED,
                        max_r,
                        art.get("file_id"),
                        art.get("file_unique_id"),
                        art.get("message_id"),
                        art.get("chat_id"),
                        now,
                        now,
                        now,
                        now,
                    ),
                )
                stats["artifacts_upserted"] += 1

        # expected count
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
    return stats


def startup_sync_manifest(db_path: Any) -> dict[str, Any]:
    """Fetch GitHub manifest and rebuild SQLite. Never raises to caller for transient errors."""
    result = {"status": "skipped", "stats": {}}
    if not manifest_enabled():
        result["status"] = "disabled"
        return result
    if not manifest_token() or not manifest_repo():
        log.warning("ARCHIVE_MANIFEST_ENABLED but token/repo missing — skip sync")
        result["status"] = "misconfigured"
        return result
    try:
        manifest, _sha, st = fetch_manifest()
        if st == "missing":
            # create empty remote? optional — leave missing until first upload
            result["status"] = "missing"
            return result
        if st != "ok" or manifest is None:
            result["status"] = st
            return result
        stats = rebuild_sqlite_from_manifest(db_path, manifest)
        result["status"] = "ok"
        result["stats"] = stats
        log.info(
            "Manifest sync OK packages=%s artifacts=%s skipped_wip=%s",
            stats.get("packages"),
            stats.get("artifacts_upserted"),
            stats.get("skipped_local_wip"),
        )
        return result
    except Exception as e:  # noqa: BLE001
        log.warning("Manifest startup sync failed (bot continues): %s", e)
        result["status"] = "error"
        return result
