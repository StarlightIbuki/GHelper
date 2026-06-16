from __future__ import annotations

import re
from types import SimpleNamespace

import click

from gh_rerunner.cli import (
    _build_pr_display_meta,
    _clean_pr_title,
    _count_approved_reviews,
    _collect_failed_jobs,
    _extract_backport_source_pr,
    _extract_backport_target_branch,
    _format_markdown_summary,
    _get_cached_pr_status,
    _pr_requirements_status,
    _render_context_lines,
    _save_session,
    _load_sessions,
    _resolve_session_ref,
    _set_cached_pr_status,
)


def test_format_markdown_summary_matches_backport_style():
    text = _format_markdown_summary(
        "Assigned PRs for @octo",
        [
            ("feature-a", "https://github.com/org/repo/pull/1", "CI pending"),
            ("feature-b", "https://github.com/org/repo/pull/2", "CI failed"),
        ],
        {"source": "assigned-prs", "assignee": "octo"},
    )

    assert text == "\n".join([
        '# Assigned PRs for @octo',
        '<!-- gh-rerunner: format="2" source="assigned-prs" assignee="octo" -->',
        '- [feature-a](https://github.com/org/repo/pull/1) CI pending',
        '- [feature-b](https://github.com/org/repo/pull/2) CI failed',
    ])


def test_render_context_lines_highlights_matches_with_adjacent_lines():
    pattern = re.compile(r"needle")
    rendered = _render_context_lines(
        "alpha\nbeta\nneedle line\ngamma\ndelta",
        pattern,
        1,
    )

    assert rendered == [
        "     2 | beta",
        f">    3 | {click.style('needle', fg='yellow', bold=True)} line",
        "     4 | gamma",
    ]


def test_collect_failed_jobs_filters_retry_conclusions():
    run = SimpleNamespace(
        jobs=lambda: [
            SimpleNamespace(conclusion="failure"),
            SimpleNamespace(conclusion="success"),
            SimpleNamespace(conclusion="timed_out"),
            SimpleNamespace(conclusion="cancelled"),
            SimpleNamespace(conclusion=None),
        ],
    )

    failed_jobs = _collect_failed_jobs(run)

    assert [job.conclusion for job in failed_jobs] == ["failure", "timed_out", "cancelled"]


def test_count_approved_reviews_uses_latest_state_per_user():
    reviews = [
        SimpleNamespace(user=SimpleNamespace(login="alice"), state="APPROVED"),
        SimpleNamespace(user=SimpleNamespace(login="bob"), state="COMMENTED"),
        SimpleNamespace(user=SimpleNamespace(login="alice"), state="CHANGES_REQUESTED"),
        SimpleNamespace(user=SimpleNamespace(login="carol"), state="APPROVED"),
    ]
    pr = SimpleNamespace(get_reviews=lambda: reviews)

    assert _count_approved_reviews(pr) == 1


def test_pr_requirements_status_checks_labels_and_reviews():
    pr = SimpleNamespace(
        labels=[SimpleNamespace(name="needs-backport"), SimpleNamespace(name="team/ci")],
        get_reviews=lambda: [
            SimpleNamespace(user=SimpleNamespace(login="alice"), state="APPROVED"),
            SimpleNamespace(user=SimpleNamespace(login="bob"), state="APPROVED"),
        ],
    )
    ok, reason = _pr_requirements_status(
        pr,
        {"required_labels": ["backport", "team/ci"], "required_reviews": 2},
    )

    assert ok is True
    assert reason == "ok"


def test_pr_requirements_status_fails_when_missing_label():
    pr = SimpleNamespace(
        labels=[SimpleNamespace(name="bugfix")],
        get_reviews=lambda: [],
    )
    ok, reason = _pr_requirements_status(
        pr,
        {"required_labels": ["release"], "required_reviews": 0},
    )

    assert ok is False
    assert "missing labels" in reason


def test_pr_status_cache_round_trip():
    cache = {"prs": {}}

    _set_cached_pr_status(
        cache,
        repo_name="org/repo",
        pr_number=123,
        branch="feature/x",
        detail="CI failed",
        title="Fix flaky test",
    )

    cached = _get_cached_pr_status(cache, "org/repo", 123, ttl_seconds=99999)

    assert cached == {
        "branch": "feature/x",
        "detail": "CI failed",
        "title": "Fix flaky test",
        "source_pr": 0,
        "backport_target": "",
    }


def test_pr_status_cache_round_trip_preserves_backport_target():
    cache = {"prs": {}}

    _set_cached_pr_status(
        cache,
        repo_name="org/repo",
        pr_number=456,
        branch="mergify/bp/release-3.11/pr-99",
        detail="CI failed",
        title="chore(backport): fix race",
        source_pr=99,
        backport_target="release-3.11",
    )

    cached = _get_cached_pr_status(cache, "org/repo", 456, ttl_seconds=99999)

    assert cached == {
        "branch": "mergify/bp/release-3.11/pr-99",
        "detail": "CI failed",
        "title": "chore(backport): fix race",
        "source_pr": 99,
        "backport_target": "release-3.11",
    }


def test_pr_status_cache_expires_by_ttl():
    cache = {
        "prs": {
            "org/repo#1": {
                "ts": 1,
                "branch": "a",
                "detail": "CI pending",
                "title": "Old",
            }
        }
    }

    # TTL is 0 sec here, so entry is always stale.
    assert _get_cached_pr_status(cache, "org/repo", 1, ttl_seconds=0) is None


def test_clean_pr_title_strips_conventional_prefix():
    assert _clean_pr_title("feat(ci): improve retries") == "improve retries"
    assert _clean_pr_title("fix!: breaking tweak") == "breaking tweak"


def test_build_pr_display_meta_marks_backport_and_branch():
    meta = _build_pr_display_meta(
        "chore(backport): add fix",
        "backport/123-to-release/3.1.x",
    )

    assert meta["pr_title"] == "add fix"
    assert meta["is_backport"] is True
    assert meta["backport_target"] == "release/3.1.x"


def test_build_pr_display_meta_keeps_unknown_target_without_branch_hints():
    meta = _build_pr_display_meta(
        "chore(backport): add fix",
        "feature/random",
    )

    assert meta["is_backport"] is True
    assert meta["backport_target"] == ""


def test_extract_backport_target_branch_bp_ref():
    assert _extract_backport_target_branch("", "mergify/bp/release-3.11/pr-15111") == "release-3.11"
    assert _extract_backport_target_branch("", "bp/main/pr-99") == "main"


def test_extract_backport_source_pr():
    assert _extract_backport_source_pr("Backport of #15111 to release/3.11") == 15111
    assert _extract_backport_source_pr("cherry-pick #42 fixes") == 42
    assert _extract_backport_source_pr("no pr here") == 0


def test_session_save_and_load(tmp_path, monkeypatch):
    import gh_rerunner.cli as cli_mod
    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(cli_mod, "_SESSION_PATH", session_file)

    idx = _save_session("https://github.com/org/repo/pull/1\n", {})
    assert idx == 1
    sessions = _load_sessions()
    assert len(sessions) == 1
    assert "https://github.com/org/repo/pull/1" in sessions[0]["raw"]


def test_session_resolve_ref(tmp_path, monkeypatch):
    import gh_rerunner.cli as cli_mod
    session_file = tmp_path / "sessions.json"
    monkeypatch.setattr(cli_mod, "_SESSION_PATH", session_file)

    _save_session("first\n", {})
    _save_session("second\n", {})

    assert _resolve_session_ref("#last") == "second\n"
    assert _resolve_session_ref("#1") == "first\n"
    assert _resolve_session_ref("#2") == "second\n"
    assert _resolve_session_ref("#99") is None
