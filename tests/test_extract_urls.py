"""Tests for gh_rerunner — stdin / backport-tracker output parsing."""
from __future__ import annotations

from typing import Optional
import pytest

from gh_rerunner.cli import _extract_urls, _parse_summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def bp_summary(*entries: tuple[str, str, str], ignore_ci: Optional[list[str]] = None) -> str:
    """Build a backport-tracker copy-summary string.

    Each entry is (status, branch, url), mirroring the JS template:
        `[${STATUS}] ${branch}: ${url}`

    Pass ``ignore_ci`` to prepend the config header line.
    """
    lines = []
    if ignore_ci:
        lines.append(f"# gh-rerunner: ignore_ci={','.join(ignore_ci)}")
    lines.extend(f"[{s}] {b}: {u}" for s, b, u in entries)
    return "\n".join(lines)


def bp_markdown_summary(
    title: str,
    pr_number: int,
    entries: list[tuple[str, str, str]],
    metadata: Optional[dict[str, str]] = None,
) -> str:
    """Build markdown copy-summary string from backport-tracker."""
    lines = [f"# Backport PRs for \"{title}\" #{pr_number}"]
    if metadata:
        attrs = " ".join(f'{k}="{v}"' for k, v in metadata.items())
        lines.append(f"<!-- gh-rerunner: {attrs} -->")
    lines.extend(f"- [{branch}]({url}) {detail}" for branch, url, detail in entries)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# _extract_urls — backport-tracker output
# ---------------------------------------------------------------------------

class TestExtractUrlsBackportSummary:
    """_extract_urls correctly parses every copy-summary variant."""

    def test_single_open_pr(self):
        text = bp_summary(("OPEN", "release-1.2", "https://github.com/org/repo/pull/101"))
        assert _extract_urls(text) == ["https://github.com/org/repo/pull/101"]

    def test_single_merged_pr(self):
        text = bp_summary(("MERGED", "release-1.3", "https://github.com/org/repo/pull/102"))
        assert _extract_urls(text) == ["https://github.com/org/repo/pull/102"]

    def test_multiple_entries_preserves_order(self):
        entries = [
            ("MERGED", "release-1.1", "https://github.com/org/repo/pull/10"),
            ("OPEN",   "release-1.2", "https://github.com/org/repo/pull/20"),
            ("FAILURE","release-1.3", "https://github.com/org/repo/pull/30"),
            ("PENDING","release-1.4", "https://github.com/org/repo/pull/40"),
        ]
        text = bp_summary(*entries)
        expected = [u for _, _, u in entries]
        assert _extract_urls(text) == expected

    def test_deduplication(self):
        """The same URL appearing twice (e.g. copy-pasted) is returned once."""
        url = "https://github.com/org/repo/pull/55"
        text = bp_summary(
            ("OPEN", "release-1.2", url),
            ("OPEN", "release-1.2", url),
        )
        assert _extract_urls(text) == [url]

    def test_deduplication_preserves_first_occurrence_order(self):
        url_a = "https://github.com/org/repo/pull/1"
        url_b = "https://github.com/org/repo/pull/2"
        text = bp_summary(
            ("OPEN",   "release-1.1", url_a),
            ("OPEN",   "release-1.2", url_b),
            ("MERGED", "release-1.1", url_a),  # duplicate
        )
        assert _extract_urls(text) == [url_a, url_b]

    def test_all_known_status_labels(self):
        """All status strings produced by backport-tracker are handled."""
        statuses = ["MERGED", "OPEN", "CLOSED", "SUCCESS", "FAILURE",
                    "PENDING", "TEST_FAIL", "REVIEW_REQUIRED", "LABEL_REQUIRED",
                    "FETCHING", "ERROR"]
        entries = [
            (s, f"release-{i}", f"https://github.com/org/repo/pull/{i}")
            for i, s in enumerate(statuses, start=1)
        ]
        text = bp_summary(*entries)
        expected = [u for _, _, u in entries]
        assert _extract_urls(text) == expected

    def test_run_url_in_summary(self):
        """An Actions run URL (not a PR URL) is also extracted correctly."""
        url = "https://github.com/org/repo/actions/runs/987654"
        text = f"[FAILURE] release-1.5: {url}"
        assert _extract_urls(text) == [url]

    def test_mixed_pr_and_run_urls(self):
        pr_url  = "https://github.com/org/repo/pull/10"
        run_url = "https://github.com/org/repo/actions/runs/999"
        text = "\n".join([
            f"[OPEN]    release-1.2: {pr_url}",
            f"[FAILURE] release-1.3: {run_url}",
        ])
        assert _extract_urls(text) == [pr_url, run_url]

    def test_trailing_newline_and_whitespace(self):
        url = "https://github.com/org/repo/pull/77"
        text = f"[OPEN] release-1.2: {url}\n\n"
        assert _extract_urls(text) == [url]

    def test_empty_string_returns_empty_list(self):
        assert _extract_urls("") == []

    def test_no_urls_returns_empty_list(self):
        assert _extract_urls("[OPEN] release-1.2: (no link yet)") == []

    def test_url_not_in_summary_format(self):
        """Bare URL without the summary prefix is still extracted."""
        url = "https://github.com/org/repo/pull/88"
        assert _extract_urls(url) == [url]

    def test_ignores_non_github_urls(self):
        text = (
            "[OPEN] release-1.2: https://gitlab.com/org/repo/pull/1\n"
            "[OPEN] release-1.3: https://github.com/org/repo/pull/2"
        )
        assert _extract_urls(text) == ["https://github.com/org/repo/pull/2"]

    def test_url_with_fragment_is_truncated_at_boundary(self):
        """URL regex stops at whitespace; hash fragments don't appear in output."""
        url  = "https://github.com/org/repo/pull/99"
        text = f"[OPEN] release-1.2: {url}#issuecomment-123"
        # The regex stops before # because # is not in [^/\s] after the num group
        assert _extract_urls(text) == [url]

    def test_real_world_multiline_summary(self):
        """Simulate a realistic full copy-summary paste."""
        text = (
            "[MERGED] release-1.14: https://github.com/acme/engine/pull/500\n"
            "[OPEN] release-1.15: https://github.com/acme/engine/pull/501\n"
            "[FAILURE] release-1.16: https://github.com/acme/engine/pull/502\n"
            "[PENDING] release-1.17: https://github.com/acme/engine/pull/503\n"
        )
        assert _extract_urls(text) == [
            "https://github.com/acme/engine/pull/500",
            "https://github.com/acme/engine/pull/501",
            "https://github.com/acme/engine/pull/502",
            "https://github.com/acme/engine/pull/503",
        ]


# ---------------------------------------------------------------------------
# _parse_summary
# ---------------------------------------------------------------------------

class TestParseSummary:
    """_parse_summary correctly parses status hints and the ignore_ci header."""

    # ── Status hints ─────────────────────────────────────────────────────────

    def test_status_hints_are_lowercased(self):
        text = bp_summary(
            ("MERGED",   "next/3.10.x", "https://github.com/org/repo/pull/10"),
            ("OPEN",     "next/3.11.x", "https://github.com/org/repo/pull/11"),
            ("FETCHING", "next/3.12.x", "https://github.com/org/repo/pull/12"),
        )
        result = _parse_summary(text)
        assert [(e.status, e.url) for e in result.entries] == [
            ("merged",   "https://github.com/org/repo/pull/10"),
            ("open",     "https://github.com/org/repo/pull/11"),
            ("fetching", "https://github.com/org/repo/pull/12"),
        ]

    def test_real_world_kong_style_input(self):
        """The exact format from the user's example."""
        text = (
            "[MERGED] next/3.10.x.x: https://github.com/Kong/kong-ee/pull/17016\n"
            "[MERGED] next/3.11.x.x: https://github.com/Kong/kong-ee/pull/17017\n"
            "[FETCHING] next/3.12.x.x: https://github.com/Kong/kong-ee/pull/17018\n"
            "[FETCHING] next/3.13.x.x: https://github.com/Kong/kong-ee/pull/17019\n"
            "[FETCHING] next/3.14.x.x: https://github.com/Kong/kong-ee/pull/17039\n"
        )
        result = _parse_summary(text)
        assert len(result.entries) == 5
        assert result.entries[0].status == "merged"
        assert result.entries[0].url == "https://github.com/Kong/kong-ee/pull/17016"
        assert result.entries[2].status == "fetching"
        assert result.entries[2].url == "https://github.com/Kong/kong-ee/pull/17018"
        assert result.ignore_ci == []
        assert result.metadata == {}

    def test_all_statuses_captured(self):
        statuses = ["MERGED", "OPEN", "CLOSED", "SUCCESS", "FAILURE",
                    "PENDING", "TEST_FAIL", "FETCHING", "ERROR"]
        entries = [
            (s, f"next/{i}.x", f"https://github.com/org/repo/pull/{i}")
            for i, s in enumerate(statuses, 1)
        ]
        result = _parse_summary(bp_summary(*entries))
        assert [e.status for e in result.entries] == [s.lower() for s in statuses]

    # ── ignore_ci header ─────────────────────────────────────────────────────

    def test_ignore_ci_header_single_job(self):
        text = bp_summary(
            ("FAILURE", "next/3.12.x", "https://github.com/org/repo/pull/1"),
            ignore_ci=["lint"],
        )
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint"]
        assert result.metadata["ignore_ci"] == "lint"

    def test_ignore_ci_header_multiple_jobs(self):
        text = bp_summary(
            ("FAILURE", "next/3.12.x", "https://github.com/org/repo/pull/1"),
            ignore_ci=["lint", "build-docs", "typecheck"],
        )
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint", "build-docs", "typecheck"]
        assert result.metadata["ignore_ci"] == "lint,build-docs,typecheck"

    def test_ignore_ci_header_with_spaces_around_commas(self):
        text = "# gh-rerunner: ignore_ci=lint , build , typecheck\n[OPEN] x: https://github.com/org/repo/pull/1"
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint", "build", "typecheck"]

    def test_ignore_ci_header_case_insensitive(self):
        text = "# GH-RERUNNER: IGNORE_CI=Lint,Build\n[OPEN] x: https://github.com/org/repo/pull/1"
        result = _parse_summary(text)
        assert result.ignore_ci == ["Lint", "Build"]

    def test_no_ignore_ci_header_returns_empty_list(self):
        text = bp_summary(("OPEN", "next/3.12.x", "https://github.com/org/repo/pull/1"))
        parsed = _parse_summary(text)
        assert parsed.ignore_ci == []
        assert parsed.metadata == {}

    def test_full_summary_with_config_header(self):
        """Combined: config header + mixed statuses, as backport-tracker would emit."""
        text = bp_summary(
            ("MERGED",   "next/3.10.x", "https://github.com/org/repo/pull/10"),
            ("MERGED",   "next/3.11.x", "https://github.com/org/repo/pull/11"),
            ("FETCHING", "next/3.12.x", "https://github.com/org/repo/pull/12"),
            ("FAILURE",  "next/3.13.x", "https://github.com/org/repo/pull/13"),
            ignore_ci=["lint", "build"],
        )
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint", "build"]
        assert len(result.entries) == 4

    def test_markdown_summary_plus_appended_bare_urls(self):
        text = bp_markdown_summary(
            title="sample fix",
            pr_number=100,
            entries=[
                ("next/1", "https://github.com/org/repo/pull/201", "CI failed"),
                ("next/2", "https://github.com/org/repo/pull/202", "Merged"),
            ],
            metadata={"ignore_ci": "lint"},
        )
        text += "\nhttps://github.com/org/repo/pull/999\nhttps://github.com/org/repo/actions/runs/8888\n"

        result = _parse_summary(text)
        assert [e.url for e in result.entries] == [
            "https://github.com/org/repo/pull/201",
            "https://github.com/org/repo/pull/202",
            "https://github.com/org/repo/pull/999",
            "https://github.com/org/repo/actions/runs/8888",
        ]
        assert [e.status for e in result.entries] == ["failure", "merged", "", ""]
        merged = [e for e in result.entries if e.status == "merged"]
        fetching = [e for e in result.entries if e.status == "fetching"]
        failure = [e for e in result.entries if e.status == "failure"]
        assert len(merged) == 1
        assert len(fetching) == 0
        assert len(failure) == 1

    def test_v2_metadata_headers_are_parsed(self):
        text = "\n".join([
            "# gh-rerunner: format=2",
            "# gh-rerunner: source_pr=https://github.com/org/repo/pull/999",
            "# gh-rerunner: source_pr_description_b64=UFIgZGVzY3JpcHRpb24=",
            "# gh-rerunner: ignore_ci=lint,build",
            "[OPEN] next/3.12.x: https://github.com/org/repo/pull/1 | ci=passed=2,failed=0",
        ])
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint", "build"]
        assert result.metadata == {
            "format": "2",
            "source_pr": "https://github.com/org/repo/pull/999",
            "source_pr_description_b64": "UFIgZGVzY3JpcHRpb24=",
            "ignore_ci": "lint,build",
        }
        assert len(result.entries) == 1
        assert result.entries[0].url == "https://github.com/org/repo/pull/1"

    def test_markdown_summary_with_meta_comment(self):
        text = bp_markdown_summary(
            title="Fix: clustering syncing issue",
            pr_number=3125,
            metadata={
                "format": "2",
                "ignore_ci": "lint,build-docs",
                "source_pr": "https://github.com/org/repo/pull/3125",
                "source_pr_description_b64": "Rml4IGRlc2NyaXB0aW9u",
            },
            entries=[
                ("next/3.11.x.x", "https://github.com/org/repo/pull/11", "Merged"),
                ("next/3.12.x.x", "https://github.com/org/repo/pull/12", "1 Review required; 1 label required; CI failed"),
                ("next/3.13.x.x", "https://github.com/org/repo/pull/13", "CI failed"),
            ],
        )
        result = _parse_summary(text)
        assert result.ignore_ci == ["lint", "build-docs"]
        assert result.metadata["format"] == "2"
        assert result.metadata["source_pr"] == "https://github.com/org/repo/pull/3125"
        assert result.metadata["source_pr_description_b64"] == "Rml4IGRlc2NyaXB0aW9u"
        assert [(e.status, e.url) for e in result.entries] == [
            ("merged", "https://github.com/org/repo/pull/11"),
            ("failure", "https://github.com/org/repo/pull/12"),
            ("failure", "https://github.com/org/repo/pull/13"),
        ]

    def test_markdown_meta_only_does_not_create_targets(self):
        text = "\n".join([
            '# Backport PRs for "Fix" #100',
            '<!-- gh-rerunner: source_pr="https://github.com/org/repo/pull/100" ignore_ci="lint" -->',
        ])
        result = _parse_summary(text)
        assert result.entries == []
        assert result.ignore_ci == ["lint"]
        assert result.metadata["source_pr"] == "https://github.com/org/repo/pull/100"

    # ── Fallback: bare URLs without status prefix ─────────────────────────────

    def test_bare_url_falls_back_to_empty_status(self):
        url = "https://github.com/org/repo/pull/99"
        result = _parse_summary(url)
        assert len(result.entries) == 1
        assert result.entries[0].url == url
        assert result.entries[0].status == ""

    def test_metadata_header_url_not_used_as_fallback_target(self):
        text = "\n".join([
            "# gh-rerunner: source_pr=https://github.com/org/repo/pull/999",
            "# gh-rerunner: format=2",
        ])
        result = _parse_summary(text)
        assert result.entries == []
        assert result.metadata["source_pr"] == "https://github.com/org/repo/pull/999"

    def test_empty_input(self):
        result = _parse_summary("")
        assert result.entries == []
        assert result.ignore_ci == []
        assert result.metadata == {}
