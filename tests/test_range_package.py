"""Unit tests for ephemeral range ZIP packaging (no network)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from engine.range_package import (
    assign_parts,
    build_package_json,
    build_range_zip_parts,
    collect_plan,
    kind_file_names,
    write_zip_part,
    DayFiles,
    PackagePlan,
)


def test_kind_file_names():
    assert "ticks.parquet" in kind_file_names("tick")
    assert "bars_1s.parquet" not in kind_file_names("tick")
    assert "bars_1s.parquet" in kind_file_names("1s")
    assert len(kind_file_names("both")) == 3


def test_assign_parts_splits(tmp_path: Path):
    f1 = tmp_path / "a.bin"
    f2 = tmp_path / "b.bin"
    f1.write_bytes(b"x" * 1000)
    f2.write_bytes(b"y" * 1000)
    dfs = [
        DayFiles(day="2024-01-01", files={"ticks.parquet": f1}),
        DayFiles(day="2024-01-02", files={"ticks.parquet": f2}),
    ]
    parts = assign_parts(dfs, "USA30IDXUSD", max_bytes=1500)
    assert len(parts) >= 2


def test_write_zip_contains_package_json_and_layout(tmp_path: Path):
    d = tmp_path / "day"
    d.mkdir()
    tick = d / "ticks.parquet"
    tick.write_bytes(b"PAR1")
    entries = [("USA30IDXUSD/2024-06-12/ticks.parquet", tick)]
    plan = PackagePlan(
        symbol="USA30IDXUSD",
        start="2024-06-12",
        end="2024-06-12",
        kind="tick",
        included_days=["2024-06-12"],
        empty_days=[],
        failed_days=[],
        skipped_days=[],
        day_files=[DayFiles(day="2024-06-12", files={"ticks.parquet": tick})],
    )
    pkg = build_package_json(plan, 1, 1, [{"path": entries[0][0], "size_bytes": 4}])
    dest = tmp_path / "out.zip"
    write_zip_part(entries, pkg, dest)
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        assert "package.json" in names
        assert "USA30IDXUSD/2024-06-12/ticks.parquet" in names
        meta = json.loads(zf.read("package.json"))
        assert meta["ephemeral"] is True
        assert meta["symbol"] == "USA30IDXUSD"
        assert meta["included_days"] == ["2024-06-12"]


def test_build_range_zip_parts_multi(tmp_path: Path):
    day_files = []
    for day in ("2024-01-01", "2024-01-02", "2024-01-03"):
        p = tmp_path / day / "ticks.parquet"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"Z" * 500)
        day_files.append(DayFiles(day=day, files={"ticks.parquet": p}))
    plan = PackagePlan(
        symbol="EURUSD",
        start="2024-01-01",
        end="2024-01-03",
        kind="tick",
        included_days=["2024-01-01", "2024-01-02", "2024-01-03"],
        empty_days=[],
        failed_days=[],
        skipped_days=[],
        day_files=day_files,
    )
    zips = build_range_zip_parts(plan, tmp_path / "out", max_bytes=45 * 1024 * 1024)
    assert len(zips) == 1
    assert zips[0].name.startswith("EURUSD_2024-01-01_2024-01-03_part")


def test_collect_plan_uses_progress_and_local(tmp_path: Path):
    from storage.paths import day_dir, ticks_parquet_path
    from datetime import date

    root = tmp_path / "data"
    day = date(2024, 6, 12)
    base = day_dir(root, provider="dukascopy", symbol="USA30IDXUSD", day=day)
    base.mkdir(parents=True)
    tp = ticks_parquet_path(base)
    tp.write_bytes(b"ticks")
    (base / "metadata.json").write_text("{}")

    plan = collect_plan(
        symbol="USA30IDXUSD",
        start="2024-06-12",
        end="2024-06-12",
        kind="tick",
        progress={"done_days": ["2024-06-12"], "empty_days": [], "failed_days": [], "skipped_days": []},
        data_root=root,
        work_dir=tmp_path / "work",
        token="",
        db_path=None,
        manifest=None,
    )
    assert plan.included_days == ["2024-06-12"]
    assert "ticks.parquet" in plan.day_files[0].files
