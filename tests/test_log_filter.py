from __future__ import annotations

from types import SimpleNamespace

import pytest

from ghelper.cli import (
    _GrepSpec,
    _extract_log_lines,
    _log_filter_preview,
    _log_filter_to_struct,
    _parse_grep_spec,
    _parse_log_filter,
    _select_failed_jobs,
    _struct_to_log_filter,
)


# A trimmed version of a real busted (Lua) failure block. The failing test is
# bounded by "__________" separator lines.
SAMPLE_LOG = """ 923.72   OK 137: degraphql plugin hybrid mode should delete, rpc_sync : off
        __________
        FAIL spec-ee/03-plugins/20-degraphql/03-hybrid_mode_spec.lua:137: degraphql ... rpc_sync : on
./spec/internal/wait.lua:86: Failed to assert eventual condition:

"UNSPECIFIED"

Result: timed out after 5.0079998970032s

5347.92 __________

------- 3 tests from spec-ee/03-plugins/20-degraphql/03-hybrid_mode_spec.lua (39593.17 ms total)
"""


def test_parse_grep_spec_peels_context_tokens():
    spec = _parse_grep_spec(r"^\s*FAIL\b  +40 -1")
    assert spec.after == 40
    assert spec.before == 1
    assert spec.pattern.pattern == r"^\s*FAIL\b"


def test_parse_grep_spec_keeps_regex_quantifier_plus():
    # A trailing "+" quantifier (\d+) must NOT be mistaken for a +N context token.
    spec = _parse_grep_spec(r"error \d+")
    assert spec.before == 0
    assert spec.after == 0
    assert spec.pattern.pattern == r"error \d+"


def test_parse_grep_spec_context_only_means_whole_log():
    spec = _parse_grep_spec("-3")
    assert spec.pattern is None
    assert spec.before == 3


def test_parse_grep_spec_empty_means_whole_log():
    spec = _parse_grep_spec("   ")
    assert spec.pattern is None


def test_parse_log_filter_comments_grep_and_jobs():
    text = "\n".join([
        "# a comment",
        "",
        r"grep: ^\s*FAIL\b +40 -1",
        r"job: .*degraphql.*  =>  ^\s*FAIL\b.*(?:\n(?!.*__________).*)*",
        "job: ^build",
        "# trailing comment",
    ])
    lf = _parse_log_filter(text)
    assert lf.global_grep.after == 40
    assert lf.global_grep.before == 1
    assert len(lf.jobs) == 2
    # First job carries a grep override; second falls back to the global grep.
    assert lf.jobs[0][1] is not None
    assert lf.jobs[1][1] is None


def test_parse_log_filter_invalid_regex_raises():
    with pytest.raises(Exception):
        _parse_log_filter("grep: (unterminated")


def test_select_failed_jobs_orders_by_selector_priority():
    lf = _parse_log_filter("\n".join([
        "job: .*degraphql.*",
        "job: ^build",
    ]))
    jobs = [
        SimpleNamespace(name="build"),
        SimpleNamespace(name="degraphql tests"),
        SimpleNamespace(name="lint"),  # not matched by any selector -> dropped
    ]
    selected = _select_failed_jobs(jobs, lf)
    assert [j.name for j, _ in selected] == ["degraphql tests", "build"]


def test_select_failed_jobs_without_selectors_returns_all():
    lf = _parse_log_filter(r"grep: ^\s*FAIL\b")
    jobs = [SimpleNamespace(name="a"), SimpleNamespace(name="b")]
    selected = _select_failed_jobs(jobs, lf)
    assert [j.name for j, _ in selected] == ["a", "b"]
    assert all(spec is lf.global_grep for _, spec in selected)


def test_extract_log_lines_block_capture_stops_at_separator():
    spec = _parse_grep_spec(r"^\s*FAIL\b.*(?:\n(?!.*__________).*)*")
    rendered = _extract_log_lines(SAMPLE_LOG, spec, color=False)
    text = "\n".join(rendered)
    # Captures from FAIL through the timeout line, but not the closing separator.
    assert any("FAIL spec-ee" in line for line in rendered)
    assert any("Result: timed out" in line for line in rendered)
    assert "__________" not in text


def test_extract_log_lines_after_before_context():
    spec = _parse_grep_spec(r"^\s*FAIL\b +2 -1")
    rendered = _extract_log_lines(SAMPLE_LOG, spec, color=False)
    # -1 before pulls in the "__________" separator above the FAIL line.
    assert rendered[0].endswith("__________")
    # The matched line is prefixed with ">".
    assert any(line.lstrip().startswith(">") or line.startswith(">") for line in rendered)


def test_extract_log_lines_whole_log_when_no_pattern():
    spec = _GrepSpec(None)
    rendered = _extract_log_lines(SAMPLE_LOG, spec, color=False)
    assert len(rendered) == len(SAMPLE_LOG.splitlines())


def test_struct_roundtrip_preserves_filter():
    text = "\n".join([
        r"grep: ^\s*FAIL\b +40 -1",
        r"job: .*degraphql.*  =>  ^\s*FAIL\b.*(?:\n(?!.*__________).*)*",
        "job: ^build",
    ]) + "\n"
    struct = _log_filter_to_struct(text)
    assert struct["global"] == {"pattern": r"^\s*FAIL\b", "before": 1, "after": 40}
    assert struct["jobs"][0]["name"] == ".*degraphql.*"
    assert struct["jobs"][0]["has_override"] is True
    assert struct["jobs"][1] == {"name": "^build", "has_override": False, "pattern": "", "before": 0, "after": 0}
    # Round-trip back to DSL and re-parse to the same struct.
    assert _log_filter_to_struct(_struct_to_log_filter(struct)) == struct


def test_struct_to_log_filter_keeps_empty_override_as_whole_log():
    struct = {"global": {"pattern": "", "before": 0, "after": 0},
              "jobs": [{"name": "^build", "has_override": True, "pattern": "", "before": 0, "after": 0}]}
    text = _struct_to_log_filter(struct)
    assert "=>" in text
    back = _log_filter_to_struct(text)
    assert back["jobs"][0]["has_override"] is True


def test_log_filter_preview_reports_matches_and_errors():
    struct = {
        "global": {"pattern": r"^\s*FAIL\b", "before": 1, "after": 40},
        "jobs": [
            {"name": ".*degraphql.*", "has_override": False, "pattern": "", "before": 0, "after": 0},
            {"name": "(bad", "has_override": False, "pattern": "", "before": 0, "after": 0},
        ],
    }
    names = ["Busted tests / Runner 7", "degraphql hybrid", "build"]
    preview = _log_filter_preview(struct, names)
    assert preview["jobs"][0]["matches"] == ["degraphql hybrid"]
    assert preview["jobs"][1]["error"]  # invalid regex reported
    assert preview["ok"] is False
    assert preview["global_error"] == ""
