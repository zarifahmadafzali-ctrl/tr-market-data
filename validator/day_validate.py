"""Read-only day/catalog validation. Never mutates SQLite or files."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

from db.catalog import connect, get_day, get_hour_statuses
from downloader.symbols import normalize_symbol
from processor.ohlc import ticks_to_seconds
from storage.parquet_io import read_bars_parquet, read_ticks_parquet
from storage.paths import bars_1s_parquet_path, day_dir, metadata_json_path, ticks_parquet_path

Severity = Literal["error", "warning"]


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class DayReport:
    symbol: str
    day: str
    provider: str
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def add(self, code: str, severity: Severity, message: str, **ctx: Any) -> None:
        self.findings.append(Finding(code, severity, message, ctx))
        if severity == "error":
            self.ok = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "day": self.day,
            "provider": self.provider,
            "ok": self.ok,
            "stats": self.stats,
            "errors": [asdict(f) for f in self.findings if f.severity == "error"],
            "warnings": [asdict(f) for f in self.findings if f.severity == "warning"],
        }


def _resolve_candidates(path_str: str | None, data_root: Path) -> list[Path]:
    if not path_str:
        return []
    raw = Path(path_str)
    cands: list[Path] = []
    if raw.is_absolute():
        cands.append(raw)
    else:
        cands.append(data_root / raw)
        parts = raw.parts
        if parts and parts[0] == "data":
            cands.append(data_root / Path(*parts[1:]))
        cands.append(data_root.parent / raw)
    # de-dupe preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in cands:
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _first_existing_file(path_str: str | None, data_root: Path) -> Path | None:
    for p in _resolve_candidates(path_str, data_root):
        try:
            if p.is_file():
                return p.resolve()
        except OSError:
            continue
    return None


def _tick_record_key(t: dict[str, Any]) -> tuple:
    def f(x: Any) -> float:
        try:
            return float(x)
        except (TypeError, ValueError):
            return float("nan")

    return (
        f(t.get("time")),
        f(t.get("bid")),
        f(t.get("ask")),
        f(t.get("bid_vol")),
        f(t.get("ask_vol")),
        f(t.get("mid")),
    )


def _validate_ticks(ticks: list[dict[str, Any]], report: DayReport) -> None:
    if not ticks:
        return
    times: list[float] = []
    for i, t in enumerate(ticks):
        try:
            tm = float(t.get("time"))
        except (TypeError, ValueError):
            report.add("BAD_TICK_TIME", "error", f"Tick[{i}] has non-numeric time", index=i)
            continue
        times.append(tm)
        try:
            bid = float(t.get("bid"))
            ask = float(t.get("ask"))
        except (TypeError, ValueError):
            report.add("BAD_TICK_PRICE", "error", f"Tick[{i}] bid/ask not numeric", index=i)
            continue
        if bid > ask:
            report.add(
                "BID_GT_ASK",
                "error",
                f"Tick[{i}] bid {bid} > ask {ask}",
                index=i,
                bid=bid,
                ask=ask,
            )

    # Non-decreasing timestamps allowed; same ms is OK.
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            report.add(
                "TICK_TIME_OUT_OF_ORDER",
                "error",
                f"Tick time decreased at index {i}: {times[i - 1]} -> {times[i]}",
                index=i,
            )
            break

    # Full-record duplicates only (not same timestamp alone).
    seen: set[tuple] = set()
    dup = 0
    for t in ticks:
        k = _tick_record_key(t)
        if k in seen:
            dup += 1
        else:
            seen.add(k)
    if dup:
        report.add(
            "DUPLICATE_TICK_RECORDS",
            "error",
            f"{dup} fully identical tick record(s) (same time+prices+volumes)",
            count=dup,
        )


def _validate_bars(bars: list[dict[str, Any]], report: DayReport) -> None:
    times: list[float] = []
    for i, b in enumerate(bars):
        try:
            tm = float(b.get("time"))
            o = float(b.get("open"))
            h = float(b.get("high"))
            low = float(b.get("low"))
            c = float(b.get("close"))
            vol = float(b.get("volume") if b.get("volume") is not None else 0)
        except (TypeError, ValueError):
            report.add("BAD_BAR_FIELDS", "error", f"Bar[{i}] has non-numeric OHLC/time", index=i)
            continue
        times.append(tm)
        if not (low <= o <= h):
            report.add(
                "OHLC_OPEN_OUT_OF_RANGE",
                "error",
                f"Bar[{i}] open not in [low, high]: L={low} O={o} H={h}",
                index=i,
            )
        if not (low <= c <= h):
            report.add(
                "OHLC_CLOSE_OUT_OF_RANGE",
                "error",
                f"Bar[{i}] close not in [low, high]: L={low} C={c} H={h}",
                index=i,
            )
        if vol < 0:
            report.add("NEGATIVE_VOLUME", "error", f"Bar[{i}] volume < 0", index=i, volume=vol)

    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            report.add(
                "BAR_TIME_OUT_OF_ORDER",
                "error",
                f"Bar time decreased at index {i}",
                index=i,
            )
            break
        if times[i] == times[i - 1]:
            report.add(
                "DUPLICATE_BAR_TIME",
                "error",
                f"Duplicate bar timestamp {times[i]} at index {i}",
                index=i,
            )


def _rebuild_compare(ticks: list[dict[str, Any]], bars: list[dict[str, Any]], report: DayReport) -> None:
    rebuilt = ticks_to_seconds(ticks, 1)
    report.stats["rebuilt_bars"] = len(rebuilt)
    if len(rebuilt) != len(bars):
        report.add(
            "REBUILD_COUNT_DIFF",
            "error",
            f"Rebuilt bar count {len(rebuilt)} != stored {len(bars)}",
            rebuilt=len(rebuilt),
            stored=len(bars),
        )
        return

    def key_bar(b: dict[str, Any]) -> tuple:
        return (
            float(b["time"]),
            round(float(b["open"]), 10),
            round(float(b["high"]), 10),
            round(float(b["low"]), 10),
            round(float(b["close"]), 10),
            round(float(b.get("volume") or 0), 10),
        )

    mismatches = 0
    for i, (a, b) in enumerate(zip(rebuilt, bars)):
        if key_bar(a) != key_bar(b):
            mismatches += 1
            if mismatches <= 5:
                report.add(
                    "REBUILD_DIFF",
                    "error",
                    f"Bar[{i}] differs from rebuild-from-ticks",
                    index=i,
                    rebuilt={k: a.get(k) for k in ("time", "open", "high", "low", "close", "volume")},
                    stored={k: b.get(k) for k in ("time", "open", "high", "low", "close", "volume")},
                )
    if mismatches > 5:
        report.add(
            "REBUILD_DIFF_MORE",
            "error",
            f"{mismatches - 5} additional bar mismatch(es) omitted",
            total_mismatches=mismatches,
        )


def _check_hours(conn, symbol: str, day: str, provider: str, row: dict[str, Any], report: DayReport) -> None:
    hours = get_hour_statuses(conn, symbol, day, provider)
    succ = sum(1 for s in hours.values() if s == "SUCCESS")
    empty = sum(1 for s in hours.values() if s == "EXPECTED_EMPTY")
    failed = sum(1 for s in hours.values() if s == "FAILED")
    other = {
        h: s
        for h, s in hours.items()
        if s not in ("SUCCESS", "EXPECTED_EMPTY", "FAILED")
    }
    report.stats["hours"] = {
        "rows": len(hours),
        "SUCCESS": succ,
        "EXPECTED_EMPTY": empty,
        "FAILED": failed,
        "other": other,
    }
    for field, got, label in (
        ("successful_hours", succ, "SUCCESS"),
        ("empty_hours", empty, "EXPECTED_EMPTY"),
        ("failed_hours", failed, "FAILED"),
    ):
        expected = int(row.get(field) or 0)
        if expected != got:
            report.add(
                "HOUR_COUNT_MISMATCH",
                "warning",
                f"download_days.{field}={expected} but hour rows {label}={got}",
                field=field,
                expected=expected,
                actual=got,
            )
    if other:
        report.add(
            "HOUR_UNUSUAL_STATUS",
            "warning",
            f"Hours with non-standard status: {other}",
            other=other,
        )


def validate_day(
    *,
    db_path: Path,
    data_root: Path,
    symbol: str,
    day: str,
    provider: str = "dukascopy",
    require_success: bool = False,
) -> DayReport:
    sym = normalize_symbol(symbol)
    report = DayReport(symbol=sym, day=day, provider=provider, ok=True)
    conn = connect(db_path)
    row = get_day(conn, sym, day, provider)
    if not row:
        report.add("NO_CATALOG_ROW", "error", "No download_days row for this symbol/day")
        return report

    report.stats["catalog"] = {
        "status": row.get("status"),
        "tick_status": row.get("tick_status"),
        "bars_1s_status": row.get("bars_1s_status"),
        "tick_count": row.get("tick_count"),
        "bars_1s_count": row.get("bars_1s_count"),
    }

    if require_success and row.get("status") != "SUCCESS":
        report.add(
            "NOT_SUCCESS",
            "warning",
            f"Day status is {row.get('status')!r}, not SUCCESS",
            status=row.get("status"),
        )

    _check_hours(conn, sym, day, provider, dict(row), report)

    # Default hive paths + catalog paths
    dpath = day_dir(data_root, provider=provider, symbol=sym, day=date.fromisoformat(day))
    tick_path = _first_existing_file(row.get("tick_file_path"), data_root) or (
        ticks_parquet_path(dpath) if ticks_parquet_path(dpath).is_file() else None
    )
    bars_path = _first_existing_file(row.get("bars_1s_file_path"), data_root) or (
        bars_1s_parquet_path(dpath) if bars_1s_parquet_path(dpath).is_file() else None
    )
    meta_path = _first_existing_file(row.get("metadata_path"), data_root) or (
        metadata_json_path(dpath) if metadata_json_path(dpath).is_file() else None
    )

    tick_needed = row.get("tick_status") == "SUCCESS" or row.get("status") == "SUCCESS"
    bars_needed = row.get("bars_1s_status") == "SUCCESS" or row.get("status") == "SUCCESS"

    if tick_needed and tick_path is None:
        report.add("MISSING_TICK_FILE", "error", "SUCCESS/tick_status requires ticks.parquet but file not found")
    if bars_needed and bars_path is None:
        report.add("MISSING_BARS_FILE", "error", "SUCCESS/bars_1s_status requires bars_1s.parquet but file not found")
    if row.get("status") == "SUCCESS" and meta_path is None:
        report.add("MISSING_METADATA", "warning", "metadata.json not found for SUCCESS day")

    ticks: list[dict[str, Any]] = []
    bars: list[dict[str, Any]] = []

    if tick_path is not None:
        try:
            ticks = read_ticks_parquet(tick_path)
            report.stats["tick_rows"] = len(ticks)
            report.stats["tick_path"] = str(tick_path)
            expected = int(row.get("tick_count") or 0)
            if expected and len(ticks) != expected:
                report.add(
                    "TICK_COUNT_MISMATCH",
                    "error",
                    f"tick_count catalog={expected} file={len(ticks)}",
                    expected=expected,
                    actual=len(ticks),
                )
            _validate_ticks(ticks, report)
        except Exception as e:
            report.add("TICK_READ_ERROR", "error", f"Cannot read ticks parquet: {e}")

    if bars_path is not None:
        try:
            bars = read_bars_parquet(bars_path)
            report.stats["bars_rows"] = len(bars)
            report.stats["bars_path"] = str(bars_path)
            expected = int(row.get("bars_1s_count") or 0)
            if expected and len(bars) != expected:
                report.add(
                    "BARS_COUNT_MISMATCH",
                    "error",
                    f"bars_1s_count catalog={expected} file={len(bars)}",
                    expected=expected,
                    actual=len(bars),
                )
            _validate_bars(bars, report)
        except Exception as e:
            report.add("BARS_READ_ERROR", "error", f"Cannot read bars parquet: {e}")

    if ticks and bars:
        _rebuild_compare(ticks, bars, report)

    if meta_path is not None:
        try:
            json.loads(meta_path.read_text(encoding="utf-8"))
            report.stats["metadata_path"] = str(meta_path)
        except Exception as e:
            report.add("METADATA_READ_ERROR", "warning", f"metadata.json unreadable: {e}")

    return report


def validate_success_days(
    *,
    db_path: Path,
    data_root: Path,
    provider: str = "dukascopy",
) -> list[DayReport]:
    conn = connect(db_path)
    cur = conn.execute(
        """
        SELECT symbol, day FROM download_days
        WHERE provider = ? AND status = 'SUCCESS'
        ORDER BY symbol, day
        """,
        (provider,),
    )
    rows = cur.fetchall()
    return [
        validate_day(
            db_path=db_path,
            data_root=data_root,
            symbol=r["symbol"],
            day=r["day"],
            provider=provider,
            require_success=True,
        )
        for r in rows
    ]


def scan_orphans(*, data_root: Path, db_path: Path, provider: str = "dukascopy") -> list[Finding]:
    """Parquet files under data_root with no matching download_days row. Read-only."""
    findings: list[Finding] = []
    if not data_root.is_dir():
        return findings
    conn = connect(db_path)
    for ticks in data_root.rglob("ticks.parquet"):
        day_folder = ticks.parent
        # Expect .../provider=X/symbol=Y/year=YYYY/month=MM/day=DD/
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in day_folder.parts if "=" in p}
        if not {"provider", "symbol", "year", "month", "day"} <= parts.keys():
            findings.append(
                Finding(
                    "ORPHAN_UNPARSEABLE_PATH",
                    "warning",
                    f"Parquet path not in expected hive layout: {ticks}",
                    {"path": str(ticks)},
                )
            )
            continue
        sym = parts["symbol"]
        day = f"{parts['year']}-{parts['month']}-{parts['day']}"
        prov = parts["provider"]
        row = get_day(conn, sym, day, prov)
        if not row:
            findings.append(
                Finding(
                    "ORPHAN_FILE",
                    "warning",
                    f"ticks.parquet has no catalog row: {sym} {day}",
                    {"path": str(ticks), "symbol": sym, "day": day},
                )
            )
    return findings
