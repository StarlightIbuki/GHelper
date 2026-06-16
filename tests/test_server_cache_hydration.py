from __future__ import annotations

import json
from pathlib import Path

from gh_rerunner.server import JSONRPCServer, _normalize_target_url


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_server_startup_rehydrates_tracker_meta_from_cache(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"

    _write_json(
        cache_path,
        {
            "prs": {
                "org/repo#123": {
                    "ts": 1,
                    "branch": "mergify/bp/release-3.11/pr-123",
                    "detail": "CI failed",
                    "title": "chore(backport): fix flaky retry",
                    "source_pr": 456,
                    "backport_target": "release-3.11",
                    "is_merged": False,
                }
            }
        },
    )

    _write_json(
        trackers_path,
        [
            {
                "id": 1,
                "target": "https://github.com/org/repo/pull/123",
                "attempts_total": 2,
                "attempts_used": 0,
                "interval_seconds": 60,
                "active": True,
                "auto_rerun": True,
                "last_detail": "",
                "last_error": "",
                "last_action": "created",
                "last_updated": 0,
                "next_check_ts": 0,
                "ignore_jobs": [],
                "repo": "",
                "pr_number": 0,
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
                "jobs": [],
            }
        ],
    )

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    trackers = server.snapshot_trackers()
    assert len(trackers) == 1
    tracker = trackers[0]
    assert tracker["repo"] == "org/repo"
    assert tracker["pr_number"] == 123
    assert tracker["last_detail"] == "CI failed"
    assert tracker["pr_title"] == "fix flaky retry"
    assert tracker["is_backport"] is True
    assert tracker["backport_target"] == "release-3.11"
    assert tracker["backport_source_pr"] == 456


def test_tracker_update_without_token_preserves_existing_meta(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"

    _write_json(
        cache_path,
        {
            "prs": {
                "org/repo#42": {
                    "ts": 1,
                    "branch": "feature/x",
                    "detail": "CI pending",
                    "title": "feat: keep cached metadata",
                    "source_pr": 0,
                    "backport_target": "",
                    "is_merged": False,
                }
            }
        },
    )

    _write_json(trackers_path, [])

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    tracker = server.submit_tracker_sync("https://github.com/org/repo/pull/42")

    # Simulate one refresh tick while unauthenticated.
    server._update_tracker(tracker)

    assert tracker["last_error"] == "Server token missing"
    assert tracker["last_action"] == "idle"
    assert tracker["last_detail"] == "CI pending"
    assert tracker["pr_title"] == "keep cached metadata"


def test_normalize_target_url_lowercases_repo_segment():
    assert (
        _normalize_target_url("https://github.com/Kong/kong-ee/pull/123")
        == "https://github.com/kong/kong-ee/pull/123"
    )
    assert (
        _normalize_target_url("https://github.com/kong/kong-ee/pull/123")
        == "https://github.com/kong/kong-ee/pull/123"
    )
    # Non-URL strings should be returned unchanged.
    assert _normalize_target_url("not-a-url") == "not-a-url"
    assert _normalize_target_url("") == ""


def test_tracker_add_collapses_case_differing_targets(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"
    _write_json(cache_path, {"prs": {}})
    _write_json(trackers_path, [])

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    first = server.submit_tracker_sync("https://github.com/Kong/kong-ee/pull/42")
    second = server.submit_tracker_sync("https://github.com/kong/kong-ee/pull/42")

    assert first["id"] == second["id"]
    assert len(server.snapshot_trackers()) == 1
    # Stored target uses the lowercase canonical form regardless of input casing.
    assert first["target"] == "https://github.com/kong/kong-ee/pull/42"


def test_load_trackers_dedupes_case_differing_targets(tmp_path: Path):
    cache_path = tmp_path / "cache.json"
    trackers_path = tmp_path / "trackers.json"
    sessions_path = tmp_path / "sessions.json"
    _write_json(cache_path, {"prs": {}})

    base = {
        "attempts_total": 2,
        "attempts_used": 0,
        "interval_seconds": 60,
        "active": True,
        "auto_rerun": True,
        "last_detail": "",
        "last_error": "",
        "last_action": "created",
        "next_check_ts": 0,
        "ignore_jobs": [],
        "repo": "",
        "pr_number": 0,
        "pr_title": "",
        "pr_base_title": "",
        "is_backport": False,
        "backport_target": "",
        "backport_source_pr": 0,
        "jobs": [],
    }

    _write_json(
        trackers_path,
        [
            {
                **base,
                "id": 1,
                "target": "https://github.com/Kong/kong-ee/pull/7",
                "last_updated": 100,
            },
            {
                **base,
                "id": 2,
                "target": "https://github.com/kong/kong-ee/pull/7",
                "last_updated": 200,
            },
        ],
    )

    server = JSONRPCServer(
        token=None,
        cache_path=cache_path,
        session_path=sessions_path,
        trackers_path=trackers_path,
    )

    trackers = server.snapshot_trackers()
    assert len(trackers) == 1
    assert trackers[0]["id"] == 2
    assert trackers[0]["target"] == "https://github.com/kong/kong-ee/pull/7"
