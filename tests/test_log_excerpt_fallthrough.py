from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import ghelper.cli as cli
import ghelper.server as server


def _make_server(tmp_path: Path) -> server.JSONRPCServer:
    d = tmp_path
    return server.JSONRPCServer(
        token="x",
        cache_path=d / "c.json",
        session_path=d / "s.json",
        trackers_path=d / "t.json",
        repo_configs_path=d / "rc.json",
    )


def test_compute_log_excerpt_falls_through_to_next_matching_job(tmp_path, monkeypatch):
    srv = _make_server(tmp_path)

    # Fake the GitHub run resolution; _collect_failed_jobs is patched below so the
    # run object only needs a name.
    srv._gh = SimpleNamespace(
        get_repo=lambda repo: SimpleNamespace(
            get_workflow_run=lambda rid: SimpleNamespace(name="Busted tests")
        )
    )

    job_a = SimpleNamespace(name="Busted tests / Runner 1")
    job_b = SimpleNamespace(name="Busted tests / Runner 2")
    logs = {
        "url-a": "all green here\nnothing to see\n",
        "url-b": "line one\n   ERR spec/foo_spec.lua:1: boom\nline three\n",
    }

    monkeypatch.setattr(server, "_collect_failed_jobs", lambda run: [job_a, job_b])
    monkeypatch.setattr(server, "_job_logs_url", lambda job: "url-a" if job is job_a else "url-b")
    monkeypatch.setattr(server, "_download_binary", lambda url, token: url.encode())
    monkeypatch.setattr(server, "_decode_log_archive", lambda blob: [("log", logs[blob.decode()])])

    # Runner 1's log has no "ERR spec" match -> should fall through to Runner 2.
    cfg = "job: Busted tests / Runner .*  =>  ERR spec +2\n"
    result = srv._compute_log_excerpt("o/r", 123, cfg)

    assert result["job_name"] == "Busted tests / Runner 2"
    assert any("ERR spec" in line for line in result["lines"])


def test_compute_log_excerpt_returns_first_job_when_none_match(tmp_path, monkeypatch):
    srv = _make_server(tmp_path)
    srv._gh = SimpleNamespace(
        get_repo=lambda repo: SimpleNamespace(
            get_workflow_run=lambda rid: SimpleNamespace(name="Busted tests")
        )
    )
    job_a = SimpleNamespace(name="Runner 1")
    job_b = SimpleNamespace(name="Runner 2")
    monkeypatch.setattr(server, "_collect_failed_jobs", lambda run: [job_a, job_b])
    monkeypatch.setattr(server, "_job_logs_url", lambda job: job.name)
    monkeypatch.setattr(server, "_download_binary", lambda url, token: b"x")
    monkeypatch.setattr(server, "_decode_log_archive", lambda blob: [("log", "no relevant content\n")])

    result = srv._compute_log_excerpt("o/r", 123, "grep: WILL_NOT_MATCH\n")
    # Nothing matched -> first job surfaced with empty lines (UI shows no-match).
    assert result["job_name"] == "Runner 1"
    assert result["lines"] == []
    assert "error" not in result
