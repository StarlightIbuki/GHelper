from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ghelper.cli import (
    _append_cache_record,
    _read_cache_jsonl,
    _write_cache_jsonl,
)
from ghelper.server import JSONRPCServer


def test_jsonl_round_trip_prs_and_runs(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    data = {
        "prs": {"o/r#1": {"ts": 1.0, "detail": "CI failed"}},
        "runs": {"99": {"ts": 2.0, "status": "completed"}},
    }
    _write_cache_jsonl(p, data)

    # One self-contained JSON object per line — no global blob.
    lines = [l for l in p.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line parses on its own

    back = _read_cache_jsonl(p)
    assert back["prs"]["o/r#1"]["detail"] == "CI failed"
    assert back["runs"]["99"]["status"] == "completed"


def test_jsonl_append_last_write_wins(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write_cache_jsonl(p, {"prs": {"o/r#1": {"ts": 1.0, "detail": "CI failed"}}, "runs": {}})
    _append_cache_record(p, "pr", "o/r#1", {"ts": 3.0, "detail": "CI passed"})
    assert _read_cache_jsonl(p)["prs"]["o/r#1"]["detail"] == "CI passed"


def test_jsonl_skips_corrupt_line(tmp_path: Path):
    p = tmp_path / "c.jsonl"
    _write_cache_jsonl(p, {"prs": {"o/r#1": {"ts": 1.0, "detail": "CI passed"}}, "runs": {}})
    with open(p, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
    # A single bad line is skipped, not fatal.
    assert _read_cache_jsonl(p)["prs"]["o/r#1"]["detail"] == "CI passed"


def test_server_appends_then_compacts_and_reloads(tmp_path: Path):
    sp = tmp_path / "s.jsonl"
    srv = JSONRPCServer(
        token=None, cache_path=sp,
        session_path=tmp_path / "sess.json", trackers_path=tmp_path / "trk.json",
    )

    async def run():
        for _ in range(300):
            await srv._push_run_status(run_id=7, status="completed", conclusion="success", workflow_name="ci")

    asyncio.run(run())
    # 300 updates to one key don't grow the log unbounded — compaction caps it at
    # the ~max(256, live) floor instead of one line per write.
    lines = [l for l in sp.read_text().splitlines() if l.strip()]
    assert len(lines) <= 257

    srv2 = JSONRPCServer(
        token=None, cache_path=sp,
        session_path=tmp_path / "sess.json", trackers_path=tmp_path / "trk.json",
    )
    assert asyncio.run(srv2._get_run_status(7))["conclusion"] == "success"


def test_server_migrates_legacy_json_blob(tmp_path: Path):
    legacy = tmp_path / "mig.json"
    legacy.write_text(json.dumps({"prs": {"o/r#5": {"ts": 1.0, "detail": "Merged", "is_merged": True}}}))
    srv = JSONRPCServer(
        token=None, cache_path=tmp_path / "mig.jsonl",
        session_path=tmp_path / "s2.json", trackers_path=tmp_path / "t2.json",
    )
    assert srv._cache["prs"]["o/r#5"]["detail"] == "Merged"
