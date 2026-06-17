"""Core CLI and polling logic for ghelper."""
from __future__ import annotations

import asyncio
import io
import importlib
import json
import os
import select
import signal
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import deque
from typing import Any, Callable, Optional

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

import click
from github import Github, GithubException
from github.WorkflowRun import WorkflowRun

try:
    _rich_console = importlib.import_module("rich.console")
    _rich_live = importlib.import_module("rich.live")
    _rich_panel = importlib.import_module("rich.panel")
    _rich_table = importlib.import_module("rich.table")
    _rich_text = importlib.import_module("rich.text")

    _RichGroup = getattr(_rich_console, "Group", None)
    _RichLive = getattr(_rich_live, "Live", None)
    _RichPanel = getattr(_rich_panel, "Panel", None)
    _RichTable = getattr(_rich_table, "Table", None)
    _RichText = getattr(_rich_text, "Text", None)
    _RICH_AVAILABLE = all(x is not None for x in (_RichGroup, _RichLive, _RichPanel, _RichTable))
except Exception:
    _RichGroup = None
    _RichLive = None
    _RichPanel = None
    _RichTable = None
    _RichText = None
    _RICH_AVAILABLE = False

# Matches GitHub PR and Actions run URLs
_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/"
    r"(?P<kind>pull|actions/runs)/(?P<num>\d+)"
)

# Conclusions that mean the run is finished cleanly — no retry needed
_DONE_CONCLUSIONS = {"success", "neutral", "skipped"}

# Conclusions that warrant a rerun
_RETRY_CONCLUSIONS = {"failure", "timed_out", "action_required", "cancelled"}

# Summary status hints that mean there is nothing left to do
_SKIP_STATUSES = {"merged", "success", "closed"}

# Summary status hints that mean CI data is not yet available
_WARN_STATUSES = {"fetching"}

# Header lines emitted by backport-tracker:
#   # ghelper: key=value
_HEADER_RE = re.compile(
    r"^#\s*ghelper:\s*(?P<key>[a-z0-9_\-]+)=(?P<value>[^\r\n]*)$",
    re.MULTILINE | re.IGNORECASE,
)

# Markdown metadata comment emitted by backport-tracker, e.g.:
#   <!-- ghelper: ignore_ci="lint,build" source_pr="..." -->
_META_COMMENT_RE = re.compile(
    r"^\s*<!--\s*ghelper:\s*(?P<body>.*?)\s*-->\s*$",
    re.IGNORECASE,
)

_META_ATTR_RE = re.compile(
    r"([a-z0-9_\-]+)\s*=\s*\"([^\"]*)\"|([a-z0-9_\-]+)\s*=\s*([^\s\"]+)",
    re.IGNORECASE,
)

# Summary entry line:  [STATUS] branch: URL
_ENTRY_RE = re.compile(
    r"^\[(?P<status>[A-Z_]+)\]\s+(?P<branch>[^:]+):\s+(?P<url>https://github\.com/\S+)\s*$",
)

# Status-prefixed line that carries no PR URL, e.g. "[MISSING] next/3.10.x.x".
# Used to surface branches that still need a backport (no PR to watch yet).
_ENTRY_NO_URL_RE = re.compile(
    r"^\[(?P<status>[A-Z_]+)\]\s+(?P<branch>.+?)\s*$",
)

# Markdown entry line: - [branch](URL) Detail text
_MD_ENTRY_RE = re.compile(
    r"^\s*-\s+\[(?P<branch>[^\]]+)\]\((?P<url>https://github\.com/\S+)\)\s*(?P<detail>.*)$",
)

_CONVENTIONAL_TITLE_PREFIX_RE = re.compile(
    r"^(?:\[[^\]]+\]\s+)?[a-zA-Z][a-zA-Z0-9_-]*(?:\([^)]+\))?(?:!)?:\s+"
)

_BACKPORT_TARGET_RE = re.compile(
    r"(?i)\b(?:backport|cherry[- ]pick)(?:\s+to)?[\s:/_-]+(?P<branch>[A-Za-z0-9._/-]+)"
)

# Mergify/bors bp/ style head refs:  bp/release-3.11/pr-15111  or  mergify/bp/release-3.11/pr-15111
_BACKPORT_BP_REF_RE = re.compile(
    r"(?:^|/)bp/(?P<branch>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)/pr-\d+$"
)

# Matches the source PR number in a backport PR description, e.g. "Backport of #15111"
_BACKPORT_SOURCE_PR_RE = re.compile(
    r"(?i)(?:backport|cherry[- ]pick)\b[^#\n]{0,60}#(\d+)"
)

_CONFIG_PATH = Path.home() / ".ghelper.json"
# Per-repo "requirements" (ignore jobs / reviews / labels, incl. backport variants)
# enforced by the server / web UI. Kept separate from ~/.ghelper.json so the export
# bundle can mirror exactly what the running server reads.
_REPO_CONFIGS_PATH = Path.home() / ".ghelper-repo-configs.json"
_PR_STATUS_CACHE_PATH = Path.home() / ".ghelper-cache.json"
_PR_STATUS_CACHE_TTL_SECONDS = 3600  # 1 hour
_SESSION_PATH = Path.home() / ".ghelper-sessions.json"
_MAX_SESSIONS = 20
_GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_DEFAULT_GITHUB_CLIENT_ID = "Ov23lio3O4l5m3CE589o"
_DEFAULT_GITHUB_CLIENT_ID_ENV = "GHELPER_GITHUB_CLIENT_ID"
_LEGACY_GITHUB_CLIENT_ID_ENVS = ("GH_RERUNNER_GITHUB_CLIENT_ID", "GH_RERUNNER_OAUTH_CLIENT_ID")
_DEFAULT_REPO_CONFIG = {
    "ignore_ci": [],
    "required_labels": [],
    "required_reviews": 0,
}


def _short_target(url: str) -> str:
    """Compact target label for terminal output."""
    m = _URL_RE.search(url)
    if not m:
        return url
    return f"{m.group('repo')}:{m.group('kind')}:{m.group('num')}"


def _infer_status_from_markdown_detail(detail: str) -> str:
    """Infer a status hint from markdown detail text."""
    d = detail.strip().lower()
    if not d:
        return ""
    if "merged" in d:
        return "merged"
    if "closed" in d:
        return "closed"
    if "ci pending" in d or "fetching" in d:
        return "fetching"
    if "ci failed" in d:
        return "failure"
    if "ci passed" in d:
        return "success"
    return ""


def _clean_pr_title(title: str) -> str:
    text = title.strip()
    if not text:
        return ""
    return _CONVENTIONAL_TITLE_PREFIX_RE.sub("", text)


def _extract_backport_target_branch(title: str, head_ref: str) -> str:
    # Try explicit backport/cherry-pick keyword in title or head ref
    for source in (title, head_ref):
        if not source:
            continue
        match = _BACKPORT_TARGET_RE.search(source)
        if match:
            branch = match.group("branch")
            # Branch patterns like 123-to-release/3.1.x should resolve to release/3.1.x.
            to_match = re.search(r"(?:^|[-_/])to[-_/](?P<branch>[A-Za-z0-9._/-]+)$", branch)
            if to_match:
                return to_match.group("branch")
            return branch

    # Mergify / bors  bp/<branch>/pr-<num>  style
    bp_match = _BACKPORT_BP_REF_RE.search(head_ref)
    if bp_match:
        return bp_match.group("branch")

    ref = head_ref.lower()
    if "backport" in ref:
        match = re.search(r"(?:to|into|for)[-_](?P<branch>[A-Za-z0-9._/-]+)", head_ref)
        if match:
            return match.group("branch")
    return ""


def _extract_backport_source_pr(body: str) -> int:
    """Return the source PR number mentioned in a backport PR description, or 0 if not found."""
    if not body:
        return 0
    m = _BACKPORT_SOURCE_PR_RE.search(body)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, IndexError):
            return 0
    return 0


def _build_pr_display_meta(title: str, head_ref: str, body: str = "") -> dict[str, Any]:
    cleaned = _clean_pr_title(title) or title.strip()
    backport_target = _extract_backport_target_branch(title, head_ref)
    lowered = f"{title} {head_ref}".lower()
    is_backport = bool(backport_target) or "backport" in lowered or "cherry-pick" in lowered

    base_title = cleaned
    if is_backport:
        base_title = re.sub(r"(?i)\bbackport\b", "", cleaned).strip(" -:_") or cleaned

    source_pr = _extract_backport_source_pr(body) if is_backport else 0

    return {
        "pr_title": cleaned,
        "pr_base_title": base_title,
        "is_backport": is_backport,
        "backport_target": backport_target,
        "backport_source_pr": source_pr,
    }


def _parse_meta_comment_attrs(body: str) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for m in _META_ATTR_RE.finditer(body):
        if m.group(1):
            attrs[m.group(1).lower()] = m.group(2)
        elif m.group(3):
            attrs[m.group(3).lower()] = m.group(4)
    return attrs


def _is_metadata_line(line: str) -> bool:
    return _HEADER_RE.match(line) is not None or _META_COMMENT_RE.match(line) is not None


def _extract_line_url(url_part: str) -> Optional[str]:
    m = _URL_RE.search(url_part)
    if not m:
        return None
    return m.group(0)


def _append_entry(
    entries: list[SummaryEntry],
    seen: set[str],
    status: str,
    url: str,
    branch: str = "",
) -> None:
    if url not in seen:
        seen.add(url)
        entries.append(SummaryEntry(status, url, branch))


def _load_user_config() -> dict[str, Any]:
    if not _CONFIG_PATH.exists():
        return {"repos": {}}
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"repos": {}}
    if not isinstance(data, dict):
        return {"repos": {}}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def _save_user_config(data: dict[str, Any]) -> None:
    if "repos" not in data or not isinstance(data["repos"], dict):
        data["repos"] = {}
    _CONFIG_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Lock down because we may store a token in here.
    try:
        os.chmod(_CONFIG_PATH, 0o600)
    except OSError:
        pass


def _resolve_saved_token() -> Optional[str]:
    """Pick the persisted token from ~/.ghelper.json (saved by `auth`)."""
    cfg = _load_user_config()
    tok = cfg.get("token") if isinstance(cfg, dict) else None
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


def _token_option_callback(
    ctx: click.Context, param: click.Parameter, value: Optional[str]
) -> str:
    """Resolve --token: explicit flag → $GITHUB_TOKEN → saved config token."""
    if value:
        return value
    saved = _resolve_saved_token()
    if saved:
        # Make it visible to anything else that reads the env var (PyGithub
        # internals, child processes, etc.) without leaking it elsewhere.
        os.environ.setdefault("GITHUB_TOKEN", saved)
        return saved
    raise click.UsageError(
        "No GitHub token available. Run `ghelper auth` to set one up, "
        "or export GITHUB_TOKEN=<token>."
    )


def _optional_token_option_callback(
    ctx: click.Context, param: click.Parameter, value: Optional[str]
) -> Optional[str]:
    """Resolve --token like _token_option_callback, but allow missing token."""
    if value:
        return value
    saved = _resolve_saved_token()
    if saved:
        os.environ.setdefault("GITHUB_TOKEN", saved)
        return saved
    return None


def _load_pr_status_cache() -> dict[str, Any]:
    if not _PR_STATUS_CACHE_PATH.exists():
        return {"prs": {}}
    try:
        data = json.loads(_PR_STATUS_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"prs": {}}
    if not isinstance(data, dict):
        return {"prs": {}}
    prs = data.get("prs")
    if not isinstance(prs, dict):
        data["prs"] = {}
    return data


def _save_pr_status_cache(data: dict[str, Any]) -> None:
    if "prs" not in data or not isinstance(data["prs"], dict):
        data["prs"] = {}
    _PR_STATUS_CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _pr_cache_key(repo_name: str, pr_number: int) -> str:
    return f"{repo_name}#{pr_number}"


def _get_cached_pr_status(
    cache_data: dict[str, Any],
    repo_name: str,
    pr_number: int,
    ttl_seconds: int = _PR_STATUS_CACHE_TTL_SECONDS,
) -> Optional[dict[str, Any]]:
    prs = cache_data.get("prs", {}) if isinstance(cache_data, dict) else {}
    if not isinstance(prs, dict):
        return None
    raw = prs.get(_pr_cache_key(repo_name, pr_number))
    if not isinstance(raw, dict):
        return None
    ts = raw.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    # Skip TTL check for merged PRs—they don't change
    is_merged = bool(raw.get("is_merged", False))
    if not is_merged and time.time() - float(ts) > ttl_seconds:
        return None
    branch = raw.get("branch")
    detail = raw.get("detail")
    title = raw.get("title", "")
    if not isinstance(branch, str) or not isinstance(detail, str) or not isinstance(title, str):
        return None
    source_pr = raw.get("source_pr", 0)
    if not isinstance(source_pr, int):
        source_pr = 0
    backport_target = raw.get("backport_target", "")
    if not isinstance(backport_target, str):
        backport_target = ""
    return {
        "branch": branch,
        "detail": detail,
        "title": title,
        "source_pr": source_pr,
        "backport_target": backport_target,
    }


def _set_cached_pr_status(
    cache_data: dict[str, Any],
    repo_name: str,
    pr_number: int,
    branch: str,
    detail: str,
    title: str,
    source_pr: int = 0,
    is_merged: bool = False,
    backport_target: str = "",
) -> None:
    prs = cache_data.setdefault("prs", {})
    if not isinstance(prs, dict):
        cache_data["prs"] = {}
        prs = cache_data["prs"]
    prs[_pr_cache_key(repo_name, pr_number)] = {
        "ts": time.time(),
        "branch": branch,
        "detail": detail,
        "title": title,
        "source_pr": source_pr,
        "is_merged": is_merged,
        "backport_target": backport_target,
    }


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

def _load_sessions() -> list[dict[str, Any]]:
    """Load saved sessions list from disk. Returns [] on error."""
    try:
        data = json.loads(_SESSION_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _save_session(raw_text: str, metadata: dict[str, str]) -> int:
    """Append a new session entry (raw summary text + metadata) and return its 1-based index."""
    sessions = _load_sessions()
    entry = {
        "ts": time.time(),
        "ts_human": time.strftime("%Y-%m-%d %H:%M:%S"),
        "raw": raw_text,
        "metadata": metadata,
    }
    sessions.append(entry)
    # Keep only the last N sessions
    if len(sessions) > _MAX_SESSIONS:
        sessions = sessions[-_MAX_SESSIONS:]
    _SESSION_PATH.write_text(json.dumps(sessions, indent=2) + "\n", encoding="utf-8")
    return len(sessions)


def _resolve_session_ref(ref: str) -> Optional[str]:
    """Resolve a session reference (#last, #N) to the stored raw text."""
    sessions = _load_sessions()
    if not sessions:
        return None
    ref = ref.lstrip("#").strip().lower()
    if ref == "last":
        return str(sessions[-1]["raw"])
    try:
        idx = int(ref)
        if 1 <= idx <= len(sessions):
            return str(sessions[idx - 1]["raw"])
    except (ValueError, IndexError):
        pass
    return None


def _repo_config(data: dict[str, Any], repo: str) -> dict[str, Any]:
    repos = data.get("repos") if isinstance(data, dict) else None
    cfg = repos.get(repo, {}) if isinstance(repos, dict) else {}
    if not isinstance(cfg, dict):
        cfg = {}
    ignore_ci = cfg.get("ignore_ci", [])
    required_labels = cfg.get("required_labels", [])
    required_reviews = cfg.get("required_reviews", 0)
    log_filter = cfg.get("log_filter", "")
    if not isinstance(ignore_ci, list):
        ignore_ci = []
    if not isinstance(required_labels, list):
        required_labels = []
    if not isinstance(required_reviews, int):
        required_reviews = 0
    if not isinstance(log_filter, str):
        log_filter = ""
    return {
        "ignore_ci": [str(x).strip() for x in ignore_ci if str(x).strip()],
        "required_labels": [str(x).strip() for x in required_labels if str(x).strip()],
        "required_reviews": max(required_reviews, 0),
        "log_filter": log_filter,
    }


def _get_repo_log_filter(repo: str) -> str:
    """Read the raw ``log_filter`` DSL text for *repo* from ~/.ghelper.json."""
    return _repo_config(_load_user_config(), repo).get("log_filter", "")


def _set_repo_log_filter(repo: str, text: str) -> None:
    """Persist the raw ``log_filter`` DSL text for *repo* into ~/.ghelper.json."""
    repo = str(repo or "").strip()
    if not repo:
        return
    cfg = _load_user_config()
    repos = cfg.setdefault("repos", {})
    if not isinstance(repos, dict):
        cfg["repos"] = {}
        repos = cfg["repos"]
    entry = repos.get(repo)
    if not isinstance(entry, dict):
        entry = {}
    entry["log_filter"] = str(text or "")
    repos[repo] = entry
    _save_user_config(cfg)


# ---------------------------------------------------------------------------
# Per-repo "requirements" (server repo-config store) + full export bundle
# ---------------------------------------------------------------------------

def _load_repo_configs_file() -> dict[str, Any]:
    if not _REPO_CONFIGS_PATH.exists():
        return {}
    try:
        data = json.loads(_REPO_CONFIGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_repo_configs_file(data: dict[str, Any]) -> None:
    _REPO_CONFIGS_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _normalize_requirements(cfg: Any) -> dict[str, Any]:
    """Coerce a requirements dict to the server's repo-config schema."""
    cfg = cfg if isinstance(cfg, dict) else {}

    def _strlist(value: Any) -> list[str]:
        return [str(x).strip() for x in (value if isinstance(value, list) else []) if str(x).strip()]

    def _nonneg(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "ignore_jobs": _strlist(cfg.get("ignore_jobs")),
        "required_reviews": _nonneg(cfg.get("required_reviews")),
        "required_labels": _strlist(cfg.get("required_labels")),
        "backport_ignore_jobs": _strlist(cfg.get("backport_ignore_jobs")),
        "backport_required_reviews": _nonneg(cfg.get("backport_required_reviews")),
        "backport_required_labels": _strlist(cfg.get("backport_required_labels")),
    }


def _get_repo_requirements(repo: str) -> dict[str, Any]:
    return _normalize_requirements(_load_repo_configs_file().get(str(repo or "").strip(), {}))


def _set_repo_requirements(repo: str, requirements: Any) -> None:
    repo = str(repo or "").strip()
    if not repo:
        return
    data = _load_repo_configs_file()
    data[repo] = _normalize_requirements(requirements)
    _save_repo_configs_file(data)


def _config_bundle(repo: str) -> dict[str, Any]:
    """Full per-repo config (requirements + log filter) for export."""
    return {
        "ghelper_config_version": 1,
        "repo": str(repo or "").strip(),
        "requirements": _get_repo_requirements(repo),
        "log_filter": _get_repo_log_filter(repo),
    }


def _apply_config_bundle(repo: str, bundle: Any) -> None:
    """Apply an exported config bundle to *repo* (validates the log filter)."""
    if not isinstance(bundle, dict):
        raise click.BadParameter("config bundle must be a JSON object")
    if "requirements" in bundle:
        _set_repo_requirements(repo, bundle.get("requirements"))
    if "log_filter" in bundle:
        text = bundle.get("log_filter") or ""
        if not isinstance(text, str):
            raise click.BadParameter("log_filter must be a string")
        _parse_log_filter(text)  # validate before persisting
        _set_repo_log_filter(repo, text)


def _target_repo(target: str, repo_opt: Optional[str]) -> Optional[str]:
    m = _URL_RE.search(target)
    if m:
        return m.group("repo")
    if target.isdigit():
        return repo_opt
    return None


def _target_pr_number(target: str) -> Optional[int]:
    m = _URL_RE.search(target)
    if not m or m.group("kind") != "pull":
        return None
    return int(m.group("num"))


def _count_approved_reviews(pr: Any) -> int:
    latest_state_by_user: dict[str, str] = {}
    for review in pr.get_reviews():
        user = getattr(getattr(review, "user", None), "login", None)
        if not user:
            continue
        latest_state_by_user[user] = str(getattr(review, "state", "")).upper()
    return sum(1 for state in latest_state_by_user.values() if state == "APPROVED")


def _pr_requirements_status(pr: Any, cfg: dict[str, Any]) -> tuple[bool, str]:
    required_labels = cfg.get("required_labels", [])
    required_reviews = int(cfg.get("required_reviews", 0) or 0)

    missing_labels: list[str] = []
    if required_labels:
        present = [str(getattr(label, "name", "")).lower() for label in getattr(pr, "labels", [])]
        for req in required_labels:
            req_l = str(req).lower()
            if not any(req_l in p for p in present):
                missing_labels.append(str(req))

    approved_count = 0
    if required_reviews > 0:
        approved_count = _count_approved_reviews(pr)

    if missing_labels:
        return False, f"missing labels: {', '.join(missing_labels)}"
    if required_reviews > 0 and approved_count < required_reviews:
        return False, f"approved reviews {approved_count}/{required_reviews}"
    return True, "ok"


def _parse_structured_line(line: str) -> Optional[tuple[str, str, str]]:
    """Parse a URL-bearing summary line into ``(status, url, branch)``."""
    legacy = _ENTRY_RE.match(line)
    if legacy:
        clean = _extract_line_url(legacy.group("url"))
        if clean:
            return legacy.group("status"), clean, legacy.group("branch").strip()

    markdown = _MD_ENTRY_RE.match(line)
    if markdown:
        clean = _extract_line_url(markdown.group("url"))
        if clean:
            status = _infer_status_from_markdown_detail(markdown.group("detail"))
            return status, clean, markdown.group("branch").strip()

    return None


def _collect_metadata(text: str) -> tuple[dict[str, str], list[str]]:
    metadata: dict[str, str] = {}
    ignore_ci: list[str] = []

    for m in _HEADER_RE.finditer(text):
        key = m.group("key").lower().strip()
        value = m.group("value").strip()
        metadata[key] = value
        if key == "ignore_ci":
            ignore_ci = [j.strip() for j in value.split(",") if j.strip()]

    for line in text.splitlines():
        c = _META_COMMENT_RE.match(line)
        if not c:
            continue
        attrs = _parse_meta_comment_attrs(c.group("body"))
        metadata.update(attrs)
        if "ignore_ci" in attrs:
            ignore_ci = [j.strip() for j in attrs["ignore_ci"].split(",") if j.strip()]

    return metadata, ignore_ci


def _collect_structured_entries(text: str) -> list[SummaryEntry]:
    entries: list[SummaryEntry] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parsed = _parse_structured_line(line)
        if not parsed:
            continue
        status, url, branch = parsed
        _append_entry(entries, seen, status, url, branch)
    return entries


def _collect_missing_entries(text: str) -> list[SummaryEntry]:
    """Status-prefixed lines that carry no PR URL, e.g. ``[MISSING] next/3.10.x.x``.

    These represent branches that still need a backport (no PR to watch yet), so
    they are surfaced separately rather than dropped or treated as watch targets.
    """
    missing: list[SummaryEntry] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        # Skip lines that already resolved to a watchable (URL-bearing) entry.
        if _parse_structured_line(line):
            continue
        if _URL_RE.search(line):
            continue
        m = _ENTRY_NO_URL_RE.match(line)
        if not m:
            continue
        branch = m.group("branch").strip()
        if not branch:
            continue
        status = m.group("status").lower()
        key = (status, branch)
        if key in seen:
            continue
        seen.add(key)
        missing.append(SummaryEntry(status, "", branch))
    return missing


def _extract_title(text: str) -> str:
    """Capture a leading freeform title line (e.g. the source-PR title).

    Returns the first non-empty content line that is not metadata, a structured
    entry, or a bare URL — stripping any leading markdown heading marker. Returns
    an empty string when the block starts straight into entries or URLs.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if _is_metadata_line(line):
            continue
        # First content line decides it: a title only if it is freeform text.
        if _ENTRY_NO_URL_RE.match(line) or _MD_ENTRY_RE.match(line):
            return ""
        if _URL_RE.search(line):
            return ""
        return re.sub(r"^#+\s*", "", stripped)
    return ""


def _format_markdown_summary(
    title: str,
    entries: list[tuple[str, str, str]],
    metadata: dict[str, str] | None = None,
) -> str:
    lines = [f"# {title}"]
    meta = {"format": "2"}
    if metadata:
        meta.update(metadata)
    attrs = " ".join(f'{key}="{value}"' for key, value in meta.items())
    lines.append(f"<!-- ghelper: {attrs} -->")
    lines.extend(f"- [{branch}]({url}) {detail}" for branch, url, detail in entries)
    return "\n".join(lines)


def _pick_pr_status(repo, pr, ignore_jobs: Optional[set[str]] = None) -> str:
    if getattr(pr, "merged", False) or getattr(pr, "merged_at", None):
        return "Merged"
    if getattr(pr, "state", "").lower() == "closed":
        return "Closed"

    try:
        runs = list(repo.get_workflow_runs(head_sha=pr.head.sha))
    except GithubException:
        return "CI unavailable"

    if not runs:
        return "CI unavailable"

    ignored: set[str] = {s.lower() for s in (ignore_jobs or set())}
    has_pending = False
    has_failure = False
    has_success = False
    for run in runs:
        if run.status != "completed":
            has_pending = True
            continue
        conclusion = (run.conclusion or "").lower()
        if conclusion in _RETRY_CONCLUSIONS:
            run_name = str(getattr(run, "name", "") or "").strip().lower()
            if run_name in ignored:
                continue
            # Also ignore when every failed job within this run is covered by
            # the ignore set.  ignore_jobs may list a job name inside a workflow
            # rather than the workflow run name itself, so we need to inspect the
            # individual jobs.  Only do the extra API call when an ignore list is
            # actually configured.
            if ignored:
                try:
                    failed = [
                        str(getattr(j, "name", "") or "").strip().lower()
                        for j in _collect_failed_jobs(run)
                    ]
                    if failed and all(f in ignored for f in failed):
                        continue
                except Exception:
                    pass
            has_failure = True
        elif conclusion in _DONE_CONCLUSIONS:
            has_success = True
        else:
            has_pending = True

    if has_failure:
        return "CI failed"
    if has_pending:
        return "CI pending"
    if has_success:
        return "CI passed"
    return "CI unavailable"


def _collect_assigned_pr_entries(
    g: Github,
    repo_opt: Optional[str] = None,
    include_closed: bool = False,
    include_drafts: bool = False,
    filter_pattern: Optional[str] = None,
    pr_status_cache: Optional[dict[str, Any]] = None,
    save_cache: Optional[Callable[[dict[str, Any]], None]] = None,
) -> tuple[str, list[tuple[str, str, str]], dict[str, str]]:
    login = g.get_user().login
    metadata: dict[str, str] = {"source": "assigned-prs", "assignee": login}
    if repo_opt:
        metadata["repo"] = repo_opt
    metadata["scope"] = "open+closed" if include_closed else "open"
    metadata["drafts"] = "included" if include_drafts else "excluded"

    filter_re = _compile_regex(filter_pattern)
    cache_data = pr_status_cache if isinstance(pr_status_cache, dict) else _load_pr_status_cache()
    cache_changed = False

    draft_qualifier = "" if include_drafts else " -is:draft"
    queries = [f"is:pr assignee:{login} is:open{draft_qualifier}"]
    if include_closed:
        queries.append(f"is:pr assignee:{login} is:closed{draft_qualifier}")
    if repo_opt:
        queries = [f"repo:{repo_opt} {query}" for query in queries]

    seen_urls: set[str] = set()
    entries: list[tuple[str, str, str]] = []
    for query_index, query in enumerate(queries, 1):
        state = "open" if "is:open" in query else "closed"
        click.echo(f"  Fetching {state} assigned PRs...", err=True)
        count = 0
        for issue in g.search_issues(query=query, sort="updated", order="desc"):
            if not getattr(issue, "pull_request", None):
                continue
            url = issue.html_url
            if url in seen_urls:
                continue
            seen_urls.add(url)
            count += 1

            detail = "CI unavailable"
            branch = getattr(issue, "title", "PR")
            title = str(getattr(issue, "title", "") or "").strip()

            # Extract repo full name from the URL to avoid a lazy-loaded API
            # call on issue.repository (which costs 1 request per PR).
            url_m = _URL_RE.search(url)
            repo_full_name = url_m.group("repo") if url_m else str(getattr(issue.repository, "full_name", ""))

            cached = _get_cached_pr_status(cache_data, repo_full_name, issue.number)
            if cached is not None:
                branch = cached["branch"] or branch
                detail = cached["detail"] or detail
                title = cached["title"] or title
                click.echo(f"    PR #{issue.number} — using cached status", err=True)
            else:
                click.echo(f"    PR #{issue.number} — fetching CI status...", err=True)
                try:
                    repo = g.get_repo(repo_full_name)
                    pr = repo.get_pull(issue.number)
                    branch = pr.head.ref or branch
                    detail = _pick_pr_status(repo, pr)
                    title = str(getattr(pr, "title", "") or title).strip()
                    body = str(getattr(pr, "body", "") or "")
                    source_pr = _extract_backport_source_pr(body)
                    is_merged = bool(getattr(pr, "merged", False))
                    base_branch = str(getattr(getattr(pr, "base", None), "ref", "") or "")
                    meta_for_cache = _build_pr_display_meta(title, branch, body)
                    backport_target_for_cache = str(meta_for_cache.get("backport_target", "") or "")
                    if meta_for_cache.get("is_backport") and not backport_target_for_cache and base_branch:
                        backport_target_for_cache = base_branch
                    _set_cached_pr_status(
                        cache_data,
                        repo_full_name,
                        issue.number,
                        branch,
                        detail,
                        title,
                        source_pr=source_pr,
                        is_merged=is_merged,
                        backport_target=backport_target_for_cache,
                    )
                    cache_changed = True
                except GithubException:
                    pass

            if filter_re and not (
                filter_re.search(branch)
                or filter_re.search(url)
                or filter_re.search(title)
                or filter_re.search(repo_full_name)
            ):
                continue

            entries.append((branch, url, detail))

        if count == 0:
            click.echo(f"    (no {state} assigned PRs found)", err=True)
        else:
            click.echo(f"    {count} {state} assigned PR(s) processed", err=True)

    title = f"Assigned PRs for @{login}"
    if cache_changed:
        if save_cache is not None:
            save_cache(cache_data)
        elif pr_status_cache is None:
            _save_pr_status_cache(cache_data)
        click.echo(f"    cache: saved to {_PR_STATUS_CACHE_PATH}", err=True)
    return title, entries, metadata


def _download_binary(url: str, token: str) -> bytes:
    # GitHub job-log URLs redirect to a pre-signed blob-storage URL whose `sig`
    # query param IS the credential. Azure/blob storage rejects requests that
    # also carry an Authorization header (HTTP 401), so only send the GitHub
    # token to GitHub hosts and fetch signed URLs bare.
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    is_github = host == "github.com" or host.endswith(".github.com")
    headers = {"User-Agent": "ghelper"}
    if is_github and token:
        headers["Authorization"] = f"Bearer {token}"
        headers["Accept"] = "application/vnd.github+json"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise click.ClickException(f"Failed to fetch logs from {url}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise click.ClickException(f"Failed to fetch logs from {url}: {exc.reason}") from exc


# ANSI/VT100 escape sequences (colors, cursor moves) embedded in CI logs.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# Per-line ISO-8601 timestamp prefix GitHub adds to raw job logs, e.g.
# "2026-06-16T11:26:05.2520732Z ".
_LOG_TS_RE = re.compile(r"(?m)^\d{4}-\d{2}-\d{2}T[0-9:.]+Z ")


def _clean_log_text(text: str) -> str:
    """Normalize a raw CI log for grepping/display.

    Strips ANSI color/escape codes and GitHub's per-line timestamp prefix so that
    patterns match the visible text (e.g. ``ERR spec`` instead of the raw
    ``ERR\\x1b[0m \\x1b[36mspec``) and ``^``-anchored patterns line up with the
    actual log content rather than the timestamp.
    """
    return _LOG_TS_RE.sub("", _ANSI_RE.sub("", text))


def _decode_log_archive(blob: bytes) -> list[tuple[str, str]]:
    if blob[:2] == b"PK":
        entries: list[tuple[str, str]] = []
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                with archive.open(name) as handle:
                    entries.append((name, _clean_log_text(handle.read().decode("utf-8", errors="replace"))))
        return entries
    return [("workflow.log", _clean_log_text(blob.decode("utf-8", errors="replace")))]


def _compile_regex(pattern: Optional[str]) -> Optional[re.Pattern[str]]:
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise click.BadParameter(f"invalid regex: {exc}") from exc


def _highlight_pattern(text: str, pattern: Optional[re.Pattern[str]]) -> str:
    if not pattern:
        return text
    return pattern.sub(lambda match: click.style(match.group(0), fg="yellow", bold=True), text)


class _GrepSpec:
    """A compiled log-grep instruction: a regex plus before/after context.

    ``pattern is None`` means "show the whole log".  ``before``/``after`` are the
    number of adjacent lines to include around each match.  A multiline regex (one
    that matches across ``\\n``) naturally spans several lines, so block capture and
    single-line + context both flow through the same renderer.
    """
    __slots__ = ("pattern", "before", "after")

    def __init__(
        self,
        pattern: Optional[re.Pattern[str]],
        before: int = 0,
        after: int = 0,
    ) -> None:
        self.pattern = pattern
        self.before = max(0, before)
        self.after = max(0, after)


class _LogFilter:
    """Parsed per-repo log-extraction config (the ``log_filter`` DSL)."""
    __slots__ = ("global_grep", "jobs")

    def __init__(
        self,
        global_grep: _GrepSpec,
        jobs: list[tuple[re.Pattern[str], Optional[_GrepSpec]]],
    ) -> None:
        self.global_grep = global_grep
        # Ordered (job-name regex, optional grep override) pairs.
        self.jobs = jobs


# Trailing context tokens in a grep spec, e.g. "+40" / "-1".
_CONTEXT_TOKEN_RE = re.compile(r"^[+-]\d+$")


def _parse_grep_spec(text: str) -> _GrepSpec:
    """Parse a ``<regex> [+N] [-N]`` grep spec.

    Trailing whitespace-separated tokens that look like ``+N`` / ``-N`` are peeled
    off as after/before context; everything before them is the regex (compiled with
    ``re.MULTILINE`` so ``^``/``$`` are per-line).  An empty regex means "whole log".
    """
    raw = (text or "").strip()
    before = 0
    after = 0
    parts = raw.split()
    n_ctx = 0
    while n_ctx < len(parts) and _CONTEXT_TOKEN_RE.match(parts[len(parts) - 1 - n_ctx]):
        value = int(parts[len(parts) - 1 - n_ctx])
        if value >= 0:
            after = value
        else:
            before = -value
        n_ctx += 1
    # Drop the trailing context tokens while preserving the regex's internal spacing.
    if n_ctx == 0:
        pattern_src = raw
    elif n_ctx >= len(parts):
        pattern_src = ""
    else:
        pattern_src = raw.rsplit(None, n_ctx)[0]
    if not pattern_src:
        return _GrepSpec(None, before, after)
    try:
        pattern = re.compile(pattern_src, re.MULTILINE)
    except re.error as exc:
        raise click.BadParameter(f"invalid grep regex {pattern_src!r}: {exc}") from exc
    return _GrepSpec(pattern, before, after)


def _parse_log_filter(text: Optional[str]) -> _LogFilter:
    """Parse the ``log_filter`` DSL into a :class:`_LogFilter`.

    Lines starting with ``#`` (and blank lines) are comments.  Recognised keys:
      ``grep: <regex> [+N] [-N]``   global grep spec (last one wins)
      ``job: <name-regex> [=> <grep override> [+N] [-N]]``  ordered job selectors
    """
    global_grep = _GrepSpec(None)
    jobs: list[tuple[re.Pattern[str], Optional[_GrepSpec]]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("grep:"):
            global_grep = _parse_grep_spec(line[len("grep:"):])
            continue
        if line.lower().startswith("job:"):
            body = line[len("job:"):].strip()
            override: Optional[_GrepSpec] = None
            if "=>" in body:
                name_src, grep_src = body.split("=>", 1)
                override = _parse_grep_spec(grep_src)
            else:
                name_src = body
            name_src = name_src.strip()
            if not name_src:
                continue
            try:
                name_re = re.compile(name_src)
            except re.error as exc:
                raise click.BadParameter(f"invalid job regex {name_src!r}: {exc}") from exc
            jobs.append((name_re, override))
    return _LogFilter(global_grep, jobs)


def _default_log_filter_template(repo: str) -> str:
    """Commented DSL template seeded into the editor when no config exists yet."""
    return (
        f"# ghelper log-extraction config for {repo or '<owner/repo>'}\n"
        "#\n"
        "# grep: <regex>  [+N] [-N]\n"
        "#   Regex applied to each failed job's log. Empty -> whole log.\n"
        "#   Trailing +N includes N lines AFTER each match, -N includes N lines BEFORE.\n"
        "#   ^ and $ are per-line (MULTILINE always on). For a multi-line block, match\n"
        "#   the start line then following lines that aren't the block terminator, e.g.\n"
        "#   capture a busted FAIL block up to the next '__________' separator:\n"
        "#     grep: ^\\s*FAIL\\b.*(?:\\n(?!.*__________).*)*\n"
        "#\n"
        "# job: <name-regex>  [=> <grep override> [+N] [-N]]\n"
        "#   Match against CI job names, in priority order. The FIRST matching failed\n"
        "#   job is shown as the first failing CI. `=> ...` overrides the global grep\n"
        "#   for that job. With no job: lines, all failed jobs use the global grep.\n"
        "#\n"
        "# Examples (uncomment and edit):\n"
        "# grep: ^\\s*FAIL\\b  +40 -1\n"
        "# job: .*degraphql.*  =>  ^\\s*FAIL\\b.*(?:\\n(?!.*__________).*)*\n"
        "# job: ^build\n"
    )


# ---------------------------------------------------------------------------
# Structured (form-friendly) view of the log_filter DSL
#
# The web UI edits the filter through a form rather than the raw DSL, but the
# DSL stays the single persisted format. These helpers convert between the DSL
# text and a JSON-able struct so the Python parser remains the source of truth
# (no duplicated regex/DSL parsing in JavaScript).
# ---------------------------------------------------------------------------

def _grep_spec_to_struct(spec: Optional[_GrepSpec]) -> dict[str, Any]:
    return {
        "pattern": spec.pattern.pattern if (spec and spec.pattern is not None) else "",
        "before": spec.before if spec else 0,
        "after": spec.after if spec else 0,
    }


def _log_filter_to_struct(text: Optional[str]) -> dict[str, Any]:
    """Parse the DSL into ``{global: {...}, jobs: [{...}]}`` for the form UI."""
    lf = _parse_log_filter(text)
    jobs: list[dict[str, Any]] = []
    for name_re, override in lf.jobs:
        entry: dict[str, Any] = {"name": name_re.pattern, "has_override": override is not None}
        entry.update(_grep_spec_to_struct(override))
        jobs.append(entry)
    return {"global": _grep_spec_to_struct(lf.global_grep), "jobs": jobs}


def _format_grep_spec_source(pattern: Any, before: Any, after: Any) -> str:
    """Render a regex + before/after back into the ``<regex> +A -B`` DSL fragment."""
    parts: list[str] = []
    p = str(pattern or "").strip()
    if p:
        parts.append(p)
    try:
        after_n = int(after or 0)
    except (TypeError, ValueError):
        after_n = 0
    try:
        before_n = int(before or 0)
    except (TypeError, ValueError):
        before_n = 0
    if after_n > 0:
        parts.append(f"+{after_n}")
    if before_n > 0:
        parts.append(f"-{before_n}")
    return " ".join(parts)


def _struct_to_log_filter(struct: Any) -> str:
    """Serialize the form struct back into DSL text (inverse of _log_filter_to_struct)."""
    if not isinstance(struct, dict):
        return ""
    lines: list[str] = []
    g = struct.get("global") or {}
    g_src = _format_grep_spec_source(g.get("pattern"), g.get("before"), g.get("after"))
    if g_src:
        lines.append(f"grep: {g_src}")
    for job in (struct.get("jobs") or []):
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "").strip()
        if not name:
            continue
        if job.get("has_override"):
            o_src = _format_grep_spec_source(job.get("pattern"), job.get("before"), job.get("after"))
            # Keep the `=>` even with an empty override (means "whole log for this job").
            lines.append(f"job: {name}  =>  {o_src}".rstrip())
        else:
            lines.append(f"job: {name}")
    return ("\n".join(lines) + "\n") if lines else ""


def _log_filter_preview(struct: Any, job_names: Any = None) -> dict[str, Any]:
    """Validate a form struct and report which tracked job names each rule matches.

    Returns ``{ok, error, text, global_error, jobs: [{name, error, matches}]}``.
    Regexes are compiled with Python's ``re`` (the same engine used at apply time)
    so validation matches real behaviour rather than JavaScript's regex dialect.
    """
    text = _struct_to_log_filter(struct)
    result: dict[str, Any] = {"ok": True, "error": "", "text": text, "global_error": "", "jobs": []}
    struct = struct if isinstance(struct, dict) else {}

    g = struct.get("global") or {}
    g_pattern = str(g.get("pattern") or "").strip()
    if g_pattern:
        try:
            re.compile(g_pattern, re.MULTILINE)
        except re.error as exc:
            result["global_error"] = str(exc)
            result["ok"] = False

    names = [str(n) for n in (job_names or []) if str(n).strip()]
    for job in (struct.get("jobs") or []):
        if not isinstance(job, dict):
            continue
        name = str(job.get("name") or "").strip()
        entry: dict[str, Any] = {"name": name, "error": "", "matches": []}
        if name:
            try:
                name_re = re.compile(name)
                entry["matches"] = [n for n in names if name_re.search(n)]
            except re.error as exc:
                entry["error"] = str(exc)
                result["ok"] = False
        if job.get("has_override"):
            o_pattern = str(job.get("pattern") or "").strip()
            if o_pattern:
                try:
                    re.compile(o_pattern, re.MULTILINE)
                except re.error as exc:
                    entry["error"] = (entry["error"] + "; " if entry["error"] else "") + f"grep: {exc}"
                    result["ok"] = False
        result["jobs"].append(entry)

    # Final guard: ensure the assembled DSL parses as a whole.
    try:
        _parse_log_filter(text)
    except Exception as exc:  # pragma: no cover - defensive
        result["ok"] = False
        if not result["error"]:
            result["error"] = str(getattr(exc, "message", exc))
    return result


def _extract_log_lines(
    text: str,
    spec: _GrepSpec,
    color: bool = True,
) -> list[str]:
    """Render the lines of *text* selected by *spec*.

    Each regex match is mapped to the line range it spans, expanded by
    ``spec.before``/``spec.after``, and overlapping ranges are merged.  Matched
    lines are prefixed with ``>``; ``...`` separates discontiguous ranges.  When
    ``spec.pattern`` is ``None`` the whole log is returned.
    """
    lines = text.splitlines()
    if not lines:
        return []

    def _highlight(line: str) -> str:
        if not color or spec.pattern is None:
            return line
        return _highlight_pattern(line, spec.pattern)

    if spec.pattern is None:
        return [f"{index + 1:>5} | {_highlight(line)}" for index, line in enumerate(lines)]

    matched_lines: set[int] = set()
    match_ranges: list[tuple[int, int]] = []
    for match in spec.pattern.finditer(text):
        start, end = match.start(), match.end()
        # An empty match still marks its line.
        last = max(start, end - 1)
        start_line = text.count("\n", 0, start)
        end_line = text.count("\n", 0, last)
        match_ranges.append((start_line, end_line))
        for li in range(start_line, end_line + 1):
            matched_lines.add(li)

    if not match_ranges:
        return []

    expanded = sorted(
        (max(0, s - spec.before), min(len(lines) - 1, e + spec.after))
        for s, e in match_ranges
    )
    merged: list[tuple[int, int]] = []
    cur_start, cur_end = expanded[0]
    for s, e in expanded[1:]:
        if s <= cur_end + 1:
            cur_end = max(cur_end, e)
        else:
            merged.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    merged.append((cur_start, cur_end))

    rendered: list[str] = []
    for range_index, (start, end) in enumerate(merged):
        if range_index > 0:
            rendered.append("    ...")
        for index in range(start, end + 1):
            prefix = ">" if index in matched_lines else " "
            rendered.append(f"{prefix}{index + 1:>5} | {_highlight(lines[index])}")
    return rendered


def _render_context_lines(
    text: str,
    pattern: Optional[re.Pattern[str]],
    context: int,
) -> list[str]:
    """Back-compat wrapper: symmetric ``context`` lines around each match."""
    return _extract_log_lines(text, _GrepSpec(pattern, before=context, after=context))


def _select_failed_jobs(
    failed_jobs: list[Any],
    log_filter: _LogFilter,
) -> list[tuple[Any, _GrepSpec]]:
    """Pair each relevant failed job with the grep spec to apply to its log.

    When the filter has ``job:`` selectors, jobs whose name matches a selector are
    surfaced first, ordered by selector priority (the first element is the "first
    failing CI"), each paired with its override-or-global grep. Selectors are a
    *preference*, not a hard filter: if none of them match any failed job, we fall
    back to all failed jobs with the global grep so the user still sees a log rather
    than an empty result. (Invalid selector regexes are rejected at config time.)
    """
    if not log_filter.jobs:
        return [(job, log_filter.global_grep) for job in failed_jobs]

    selected: list[tuple[Any, _GrepSpec]] = []
    used: set[int] = set()
    for name_re, override in log_filter.jobs:
        for index, job in enumerate(failed_jobs):
            if index in used:
                continue
            if name_re.search(str(getattr(job, "name", "") or "")):
                used.add(index)
                selected.append((job, override or log_filter.global_grep))
    if not selected:
        # No configured selector matched — degrade gracefully to all failed jobs.
        return [(job, log_filter.global_grep) for job in failed_jobs]
    return selected


def _collect_failed_jobs(run: Any) -> list[Any]:
    return [
        job for job in run.jobs()
        if (job.conclusion or "").lower() in _RETRY_CONCLUSIONS
    ]


def _job_logs_url(job: Any) -> str:
    """Resolve a WorkflowJob's logs URL across PyGithub versions.

    Newer PyGithub exposes ``logs_url`` as a method that performs a request and
    returns the (pre-signed) redirect location; older versions expose it as a
    plain string attribute. Handle both so the download gets a real URL rather
    than a bound method.
    """
    attr = getattr(job, "logs_url", None)
    if callable(attr):
        return str(attr())
    return str(attr or "")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_urls(text: str) -> list[str]:
    """Pull all GitHub PR / run URLs out of an arbitrary block of text,
    preserving order and deduplicating."""
    seen: set[str] = set()
    result: list[str] = []
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


class SummaryEntry:
    __slots__ = ("status", "url", "branch")

    def __init__(self, status: str, url: str, branch: str = "") -> None:
        self.status = status.lower()
        self.url = url
        self.branch = branch


class ParsedSummary:
    """Result of parsing a backport-tracker copy-summary block."""
    __slots__ = ("entries", "ignore_ci", "metadata", "title", "missing")

    def __init__(
        self,
        entries: list[SummaryEntry],
        ignore_ci: list[str],
        metadata: dict[str, str],
        title: str = "",
        missing: Optional[list[SummaryEntry]] = None,
    ) -> None:
        self.entries = entries
        self.ignore_ci = ignore_ci
        self.metadata = metadata
        # Freeform title line (e.g. the source-PR title), if present.
        self.title = title
        # Status-prefixed entries that carry no PR URL (e.g. [MISSING] rows).
        self.missing = missing if missing is not None else []


def _parse_summary(text: str) -> ParsedSummary:
    """Parse a backport-tracker copy-summary block.

    Extracts:
        - Per-PR status hints and URLs from either format:
            - Legacy: ``[STATUS] branch: url``
            - Markdown: ``- [branch](url) Detail text``
        - Optional metadata from either format:
            - ``# ghelper: key=value``
            - ``<!-- ghelper: key="value" -->``

    For plain URL lists (no status prefix) each URL is returned with
    status='' so the caller treats them as unknown.
    """
    # --- Config / metadata headers ---
    metadata, ignore_ci = _collect_metadata(text)

    # --- Structured entries (legacy + markdown) ---
    entries = _collect_structured_entries(text)

    # Also include bare URLs appended after a summary block.
    # Ignore metadata header lines so source_pr URLs don't become watch targets.
    # For URLs already present in structured entries, keep structured status hints.
    content_without_headers = "\n".join(
        line for line in text.splitlines()
        if not _is_metadata_line(line)
    )
    seen_urls = {e.url for e in entries}
    for url in _extract_urls(content_without_headers):
        if url not in seen_urls:
            entries.append(SummaryEntry("", url))
            seen_urls.add(url)

    # --- Branches without a PR yet + leading title line ---
    missing = _collect_missing_entries(text)
    title = metadata.get("source_pr_title", "").strip() or _extract_title(text)

    return ParsedSummary(entries, ignore_ci, metadata, title=title, missing=missing)


def _all_failures_ignored(run: WorkflowRun, ignore_ci: list[str]) -> bool:
    """Return True if every failed job in *run* matches an ignore_ci pattern."""
    if not ignore_ci:
        return False
    failed_jobs = [
        job for job in run.jobs()
        if job.conclusion in {"failure", "timed_out"}
    ]
    if not failed_jobs:
        return False
    return all(
        any(pat.lower() in job.name.lower() for pat in ignore_ci)
        for job in failed_jobs
    )


def _resolve_pr_runs(repo, pr_number: int) -> list[WorkflowRun]:
    """Return all retryable (or still in-progress) workflow runs for a PR's
    head commit. Returns an empty list when CI is already successful so the
    caller can skip watching this PR target."""
    pr = repo.get_pull(pr_number)
    sha = pr.head.sha
    live: list[WorkflowRun] = [
        r for r in repo.get_workflow_runs(head_sha=sha)
        if r.status != "completed" or r.conclusion in _RETRY_CONCLUSIONS
    ]
    return live


def _resolve_target(
    target: str, repo_opt: Optional[str], g: Github
) -> list[WorkflowRun]:
    """Parse a target string into one or more WorkflowRun objects."""
    m = _URL_RE.search(target)
    if m:
        repo = g.get_repo(m.group("repo"))
        kind, num = m.group("kind"), int(m.group("num"))
        if kind == "actions/runs":
            return [repo.get_workflow_run(num)]
        return _resolve_pr_runs(repo, num)  # pull URL

    if target.isdigit():
        if not repo_opt:
            raise click.UsageError(
                f"--repo / -R is required when the target is a bare run ID ({target!r})."
            )
        return [g.get_repo(repo_opt).get_workflow_run(int(target))]

    raise click.UsageError(f"Cannot parse target: {target!r}")


def _should_fallback_to_full_rerun(exc: GithubException) -> bool:
    message = _exc_message(exc).lower()
    return "created over a month ago" in message


def _trigger_rerun(run: WorkflowRun) -> str:
    """Try rerunning failed jobs; fallback to full rerun for known API limitations."""
    if hasattr(run, "rerun_failed_jobs"):
        try:
            run.rerun_failed_jobs()
            return "failed_jobs"
        except GithubException as exc:
            if not _should_fallback_to_full_rerun(exc):
                raise
            run.rerun()
            return "full"

    run.rerun()
    return "full"


def _trigger_update_branch(pr: Any) -> str:
    """Update a PR's branch with the latest changes from its base branch.

    Mirrors GitHub's "Update branch" button: the update-branch endpoint merges
    the base-branch HEAD into the PR branch. Returns the mode used.
    """
    pr.update_branch()
    return "update_branch"


def _exc_message(exc: GithubException) -> str:
    if isinstance(exc.data, dict):
        return exc.data.get("message", str(exc))
    return str(exc)


# ---------------------------------------------------------------------------
# Token creation deep-links
# ---------------------------------------------------------------------------

# Fine-grained PAT — pre-fills description; user must pick repo + grant Actions write
_FINE_GRAINED_BASE_URL = "https://github.com/settings/personal-access-tokens/new"
# Classic PAT — pre-selects the `repo` scope (which includes actions write)
_CLASSIC_URL = (
    "https://github.com/settings/tokens/new"
    "?scopes=repo&description=ghelper"
)


def _build_fine_grained_url(org: Optional[str] = None) -> str:
    """Build the fine-grained PAT creation URL, optionally scoped to an org."""
    params: dict[str, str] = {"description": "ghelper"}
    if org:
        # resource_owner pre-selects the org/user on the consent page so the
        # token can access repos that aren't owned by the authenticated user.
        params["resource_owner"] = org
    return _FINE_GRAINED_BASE_URL + "?" + urllib.parse.urlencode(params)


def _device_flow_client_id() -> str:
    """Read the GitHub OAuth app client ID used for device authorization."""
    client_id = os.environ.get(_DEFAULT_GITHUB_CLIENT_ID_ENV, "").strip()
    if not client_id:
        for _legacy_env in _LEGACY_GITHUB_CLIENT_ID_ENVS:
            client_id = os.environ.get(_legacy_env, "").strip()
            if client_id:
                break
    if not client_id:
        client_id = _DEFAULT_GITHUB_CLIENT_ID
    if not client_id:
        raise click.ClickException("Device flow requires a GitHub OAuth client ID.")
    return client_id


def _request_device_code(client_id: str) -> dict[str, Any]:
    """Start GitHub's device authorization flow and return the code payload."""
    payload = urllib.parse.urlencode({"client_id": client_id, "scope": "repo"}).encode("utf-8")
    request = urllib.request.Request(
        _GITHUB_DEVICE_CODE_URL,
        data=payload,
        headers={"Accept": "application/json", "User-Agent": "ghelper"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise click.ClickException(f"Unable to start device authorization with GitHub: {exc.reason or exc}")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise click.ClickException("Unexpected device-flow response from GitHub.")
    error = data.get("error")
    if error:
        description = data.get("error_description") or str(error)
        raise click.ClickException(f"GitHub device flow failed: {description}")
    return data


def _poll_device_token(client_id: str, device_code: str, interval: int, expires_in: int) -> str:
    """Poll GitHub until the user approves the device flow or it expires."""
    deadline = time.monotonic() + max(1, expires_in)
    current_interval = max(1, interval)
    last_network_error: Optional[str] = None
    while time.monotonic() < deadline:
        time.sleep(current_interval)
        payload = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            _GITHUB_OAUTH_TOKEN_URL,
            data=payload,
            headers={"Accept": "application/json", "User-Agent": "ghelper"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            last_network_error = str(exc.reason or exc)
            current_interval = min(current_interval + 5, 60)
            continue
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise click.ClickException("Unexpected device-token response from GitHub.")
        token = data.get("access_token")
        if isinstance(token, str) and token.strip():
            return token.strip()
        error = data.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            current_interval += 5
            continue
        if error == "expired_token":
            raise click.ClickException("The device authorization expired. Run `ghelper auth` again.")
        description = data.get("error_description") or (str(error) if error else "unknown error")
        raise click.ClickException(f"GitHub device flow failed: {description}")
    if last_network_error:
        raise click.ClickException(
            "Timed out waiting for device authorization. "
            f"Last network error: {last_network_error}"
        )
    raise click.ClickException("Timed out waiting for device authorization.")


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def main() -> None:
    """Headless GitHub Actions auto-rerunner.\n
    \b
    Run `ghelper auth` to get a link for creating a token with the
    minimum required permissions.
    """


# ---------------------------------------------------------------------------
# auth subcommand
# ---------------------------------------------------------------------------

@main.command("auth")
@click.option(
    "--device/--pat",
    "use_device",
    default=True,
    help="Use GitHub device authorization flow (default) or fall back to PAT mode.",
)
@click.option(
    "--classic", is_flag=True, default=False,
    help="Use the classic-PAT consent page instead of the fine-grained one.",
)
@click.option(
    "--org", "org", default=None, metavar="ORG_OR_USER",
    help=(
        "Pre-select a resource owner (org or user) on the fine-grained PAT "
        "consent page.  Required to grant access to repos that are owned by "
        "an organisation rather than your personal account."
    ),
)
@click.option(
    "--no-browser", is_flag=True, default=False,
    help="Don't auto-open the browser; just print the URL.",
)
def auth_cmd(use_device: bool, classic: bool, org: Optional[str], no_browser: bool) -> None:
    """Guided sign-in: open GitHub's token consent page, accept the token, save it.

    \b
    Behind the scenes:
      1. uses GitHub's device authorization flow by default, or opens the
         PAT consent page when --pat is selected;
      2. validates the resulting token against the GitHub API;
        3. saves it to ~/.ghelper.json (chmod 600) so future `ghelper
         watch / ls / logs` invocations pick it up without --token /
         GITHUB_TOKEN.

    \b
    Organisation repos:
      Fine-grained PATs default to your personal account as the resource
      owner, which means they cannot access repos owned by an org.  Pass
      --org ORG with --pat to pre-select the org on the consent page:

        ghelper auth --org my-company
    """
    if use_device and (classic or org):
        raise click.UsageError("--classic and --org are PAT-only options; add --pat to use them.")

    if use_device:
        client_id = _device_flow_client_id()
        device_data = _request_device_code(client_id)
        url = str(device_data.get("verification_uri_complete") or device_data.get("verification_uri") or "")
        user_code = str(device_data.get("user_code") or "")
        if not url:
            raise click.ClickException("GitHub did not provide a verification URI for the device flow.")
        if not device_data.get("device_code"):
            raise click.ClickException("GitHub did not provide a device code.")
        scope_help = "device flow scope: repo"
    else:
        url = _CLASSIC_URL if classic else _build_fine_grained_url(org)
        scope_help = (
            "scope: repo (Actions write is bundled in)"
            if classic
            else (
                f"scope: Actions → Read and write, resource owner: {org}"
                if org
                else "scope: Actions → Read and write, on the target repo only"
            )
        )

    click.echo("\n── ghelper sign-in ──────────────────────────────────────")
    click.echo(f"Opening GitHub consent page ({scope_help}):")
    click.echo(f"  {url}")

    if use_device:
        if not no_browser:
            try:
                webbrowser.open(url, new=2)
                click.echo("(browser opened)")
            except Exception:
                click.echo("(could not auto-open browser — paste the URL manually)")

        click.echo()
        click.echo("GitHub will show a short code to enter in the browser. If it does not open automatically, visit the URL above and enter:")
        click.echo(f"  {user_code}")
        click.echo("Waiting for device authorization…")

        token = _poll_device_token(
            client_id,
            str(device_data.get("device_code") or ""),
            int(device_data.get("interval") or 5),
            int(device_data.get("expires_in") or 900),
        )
        login = Github(token).get_user().login
    else:
        # --- classic terminal-prompt flow ---------------------------------
        if not no_browser:
            try:
                webbrowser.open(url, new=2)
                click.echo("(browser opened)")
            except Exception:
                click.echo("(could not auto-open browser — paste the URL manually)")
        click.echo()
        click.echo("Create the token, copy it, then paste it below.")

        token = click.prompt(
            "Paste token",
            hide_input=True,
            confirmation_prompt=False,
            type=str,
        ).strip()
        if not token:
            raise click.ClickException("Empty token — aborting.")

        click.echo("Validating with GitHub...")
        try:
            login = Github(token).get_user().login
        except GithubException as exc:
            raise click.ClickException(
                f"Token rejected by GitHub ({_exc_message(exc)}). "
                "Double-check the scope (Actions: Read and write) and try again."
            )

    cfg = _load_user_config()
    cfg["token"] = token
    _save_user_config(cfg)

    # Make it visible to anything else still running in this process.
    os.environ["GITHUB_TOKEN"] = token

    click.echo(f"\n✓ Authenticated as {login}.")
    click.echo(f"  Token saved to {_CONFIG_PATH} (chmod 600).")
    click.echo("  Subsequent `ghelper` commands pick it up automatically.")
    click.echo("  To export it into your shell as well, run:")
    click.echo(f"    export GITHUB_TOKEN={token[:6]}…")
    click.echo()


# ---------------------------------------------------------------------------
# config subcommands
# ---------------------------------------------------------------------------

@main.group("config")
def config_group() -> None:
    """Manage persistent per-repo defaults stored in ~/.ghelper.json."""


@config_group.command("show")
@click.option("-R", "--repo", "repo_opt", default=None, metavar="OWNER/REPO", help="Show config for one repo only.")
def config_show_cmd(repo_opt: Optional[str]) -> None:
    cfg = _load_user_config()
    repos = cfg.get("repos", {}) if isinstance(cfg, dict) else {}
    if repo_opt:
        one = _repo_config(cfg, repo_opt)
        click.echo(json.dumps({"path": str(_CONFIG_PATH), "repo": repo_opt, "config": one}, indent=2))
        return
    saved_token = cfg.get("token") if isinstance(cfg, dict) else None
    token_state = (
        f"{str(saved_token)[:4]}…(saved)" if isinstance(saved_token, str) and saved_token else "(none)"
    )
    click.echo(json.dumps(
        {"path": str(_CONFIG_PATH), "token": token_state, "repos": repos},
        indent=2,
        sort_keys=True,
    ))


@config_group.command("set")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository key to configure.")
@click.option("--ignore", "ignore_ci", multiple=True, metavar="JOB", help="CI job substring to ignore. Repeatable.")
@click.option("--required-label", "required_labels", multiple=True, metavar="LABEL", help="Required PR label substring. Repeatable.")
@click.option("--required-reviews", default=None, type=click.IntRange(0, 100), help="Required number of approvals.")
def config_set_cmd(
    repo_opt: str,
    ignore_ci: tuple[str, ...],
    required_labels: tuple[str, ...],
    required_reviews: Optional[int],
) -> None:
    if not ignore_ci and not required_labels and required_reviews is None:
        raise click.UsageError("Provide at least one setting to update.")

    cfg = _load_user_config()
    repos = cfg.setdefault("repos", {})
    if not isinstance(repos, dict):
        cfg["repos"] = {}
        repos = cfg["repos"]

    current = _repo_config(cfg, repo_opt)
    if ignore_ci:
        current["ignore_ci"] = [x.strip() for x in ignore_ci if x.strip()]
    if required_labels:
        current["required_labels"] = [x.strip() for x in required_labels if x.strip()]
    if required_reviews is not None:
        current["required_reviews"] = required_reviews

    repos[repo_opt] = current
    _save_user_config(cfg)
    click.echo(f"Saved config for {repo_opt} at {_CONFIG_PATH}")


@config_group.command("clear")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository key to remove.")
def config_clear_cmd(repo_opt: str) -> None:
    cfg = _load_user_config()
    repos = cfg.get("repos", {}) if isinstance(cfg, dict) else {}
    if isinstance(repos, dict) and repo_opt in repos:
        repos.pop(repo_opt, None)
        _save_user_config(cfg)
        click.echo(f"Removed config for {repo_opt} from {_CONFIG_PATH}")
    else:
        click.echo(f"No saved config for {repo_opt}")


@config_group.command("edit-log-filter")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository to edit the log filter for.")
def config_edit_log_filter_cmd(repo_opt: str) -> None:
    """Open $EDITOR to edit the per-repo failed-CI log-extraction config.

    The config controls how `ghelper logs` / the TUI / the web UI grep failed-job
    logs (see the comment header in the editor for the DSL).
    """
    current = _get_repo_log_filter(repo_opt)
    seed = current if current.strip() else _default_log_filter_template(repo_opt)
    edited = click.edit(text=seed, extension=".conf")
    if edited is None:
        click.echo("No changes (editor exited without saving).")
        return
    # Validate by parsing before persisting; surfaces bad regexes early.
    _parse_log_filter(edited)
    _set_repo_log_filter(repo_opt, edited)
    click.echo(f"Saved log filter for {repo_opt} to {_CONFIG_PATH}")


@config_group.command("export-log-filter")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository to export the log filter for.")
def config_export_log_filter_cmd(repo_opt: str) -> None:
    """Print the repo's failed-CI log filter (DSL) to stdout.

    \b
    Pipe it to a file to share or back up:
      ghelper config export-log-filter -R owner/repo > filter.conf
    """
    text = _get_repo_log_filter(repo_opt)
    if not text.strip():
        text = _default_log_filter_template(repo_opt)
    click.echo(text, nl=not text.endswith("\n"))


@config_group.command("import-log-filter")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository to import the log filter into.")
@click.argument("source", type=click.File("r"), default="-", metavar="[FILE]")
def config_import_log_filter_cmd(repo_opt: str, source: Any) -> None:
    """Import a failed-CI log filter (DSL) from FILE or stdin (validated).

    \b
      ghelper config import-log-filter -R owner/repo filter.conf
      cat filter.conf | ghelper config import-log-filter -R owner/repo
    """
    text = source.read()
    # Validate before persisting; surfaces bad regexes early.
    _parse_log_filter(text)
    _set_repo_log_filter(repo_opt, text)
    click.echo(f"Imported log filter for {repo_opt} into {_CONFIG_PATH}")


@config_group.command("export")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository to export the config for.")
def config_export_cmd(repo_opt: str) -> None:
    """Print the repo's full config (requirements + log filter) as JSON.

    \b
    Includes the ignore/review/label requirements and the failed-CI log filter.
    Pipe to a file to share or back up:
      ghelper config export -R owner/repo > config.json
    """
    click.echo(json.dumps(_config_bundle(repo_opt), indent=2, sort_keys=True))


@config_group.command("import")
@click.option("-R", "--repo", "repo_opt", required=True, metavar="OWNER/REPO", help="Repository to import the config into.")
@click.argument("source", type=click.File("r"), default="-", metavar="[FILE]")
def config_import_cmd(repo_opt: str, source: Any) -> None:
    """Import a full config bundle (JSON) from FILE or stdin (validated).

    \b
      ghelper config import -R owner/repo config.json
      cat config.json | ghelper config import -R owner/repo
    """
    try:
        bundle = json.loads(source.read())
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"invalid JSON: {exc}")
    _apply_config_bundle(repo_opt, bundle)
    click.echo(f"Imported config for {repo_opt}")


# ---------------------------------------------------------------------------
# assigned-prs subcommand
# ---------------------------------------------------------------------------

@main.command("ls")
@click.option(
    "-t", "--token",
    envvar="GITHUB_TOKEN",
    default=None,
    callback=_optional_token_option_callback,
    help="GitHub PAT (env: GITHUB_TOKEN; falls back to saved token from `auth`). Optional for --serve login flow.",
)
@click.option(
    "-R", "--repo", "repo_opt",
    default=None,
    metavar="OWNER/REPO",
    help="Optional repository scope.",
)
@click.option(
    "--include-closed",
    is_flag=True,
    default=False,
    help="Include closed PRs (default: open only).",
)
@click.option(
    "--include-drafts",
    is_flag=True,
    default=False,
    help="Include draft PRs (default: skip drafts).",
)
@click.option(
    "--filter", "filter_pattern",
    default=None,
    metavar="REGEX",
    help="Regex filter against branch/title/url/repo.",
)
def assigned_prs_cmd(
    token: str,
    repo_opt: Optional[str],
    include_closed: bool,
    include_drafts: bool,
    filter_pattern: Optional[str],
) -> None:
    """Export assigned PRs in the same markdown format used by backport-tracker."""
    g = Github(token)
    click.echo("Fetching assigned PRs...", err=True)
    try:
        title, entries, metadata = _collect_assigned_pr_entries(
            g,
            repo_opt=repo_opt,
            include_closed=include_closed,
            include_drafts=include_drafts,
            filter_pattern=filter_pattern,
        )
    except GithubException as exc:
        raise click.ClickException(
            f"GitHub API rejected the request ({_exc_message(exc)}). "
            "Check that your token is valid and has Actions:read scope."
        )
    click.echo(f"Found {len(entries)} assigned PR(s)", err=True)
    if not entries:
        click.echo(f"# {title}")
        click.echo(f"<!-- ghelper: format=\"2\" source=\"assigned-prs\" assignee=\"{g.get_user().login}\" -->")
        click.echo("No assigned PRs found.")
        return
    click.echo(f"Exporting to markdown...", err=True)
    click.echo(_format_markdown_summary(title, entries, metadata))


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------

@main.command("watch")
@click.argument("targets", nargs=-1, metavar="[TARGETS]...")
@click.option(
    "-t", "--token",
    envvar="GITHUB_TOKEN",
    default=None,
    callback=_optional_token_option_callback,
    help="GitHub PAT (env: GITHUB_TOKEN; falls back to saved token from `auth`). Optional for --serve login flow.",
)
@click.option(
    "-R", "--repo", "repo_opt",
    default=None,
    metavar="OWNER/REPO",
    help="Repo (owner/repo) — required only for bare workflow run IDs.",
)
@click.option(
    "-n", "--retries",
    "max_retries",
    type=int,
    default=3,
    show_default=True,
    help="Maximum rerun attempts per run before giving up.",
)
@click.option(
    "-i", "--interval",
    type=int,
    default=30,
    show_default=True,
    help="Server-side polling interval, seconds.",
)
@click.option(
    "--ignore",
    "ignore_ci",
    multiple=True,
    metavar="JOB",
    help="Substring of a CI job name to ignore. Repeatable.",
)
@click.option(
    "-a", "--assigned",
    is_flag=True,
    default=False,
    help="Watch PRs assigned to the current GitHub user.",
)
@click.option(
    "--filter",
    "filter_pattern",
    default=None,
    metavar="REGEX",
    help="Regex filter applied to --assigned PRs (branch/title/url/repo).",
)
@click.option(
    "--include-closed",
    is_flag=True,
    default=False,
    help="In --assigned mode, include closed PRs (default: open only).",
)
@click.option(
    "--include-drafts",
    is_flag=True,
    default=False,
    help="In --assigned mode, include draft PRs (default: skip drafts).",
)
@click.option(
    "--serve",
    is_flag=True,
    default=False,
    help="Expose HTTP/JSON-RPC + web UI on --host:--port.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="HTTP bind host when --serve is set.",
)
@click.option(
    "--port",
    type=int,
    default=53210,
    show_default=True,
    help="HTTP bind port when --serve is set.",
)
@click.option(
    "--no-tui",
    is_flag=True,
    default=False,
    help="Skip the Rich dashboard (use with --serve for a headless server).",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress streaming event log when running without a TUI.",
)
def run_cmd(
    targets: tuple[str, ...],
    token: Optional[str],
    repo_opt: Optional[str],
    max_retries: int,
    interval: int,
    ignore_ci: tuple[str, ...],
    assigned: bool,
    filter_pattern: Optional[str],
    include_closed: bool,
    include_drafts: bool,
    serve: bool,
    host: str,
    port: int,
    no_tui: bool,
    quiet: bool,
) -> None:
    """Supervise PRs / workflow runs and auto-rerun failed jobs.

    \b
    TARGETS can be any mix of:
      Run URL    https://github.com/owner/repo/actions/runs/12345
      PR URL     https://github.com/owner/repo/pull/456
      Run ID     12345           (requires -R owner/repo)
      Session    #last, #3       (resume previous invocation)

    \b
    Markdown summaries on stdin are accepted too — e.g. backport-tracker output:
      <!-- ghelper: ignore_ci="lint,build" -->
      - [release-1.2](https://github.com/owner/repo/pull/456) CI failed
      - [release-1.3](https://github.com/owner/repo/pull/457) Merged

    \b
    Quick-start:
      ghelper watch https://github.com/owner/repo/actions/runs/12345
      ghelper watch -R owner/repo 12345 -n 5
      pbpaste | ghelper watch
      cat summary.txt | ghelper watch -n 5 -i 60
      ghelper watch -a                (watch all assigned PRs)
      ghelper watch #last             (resume most recent session)
      ghelper watch --serve --no-tui  (headless server + web UI)
    """
    g: Optional[Github] = Github(token) if token else None
    user_cfg = _load_user_config()
    pr_status_cache = _load_pr_status_cache()
    pr_cache_changed = False

    # --- Collect raw summary text from all input sources ---
    raw_text_parts: list[str] = []

    if not sys.stdin.isatty():
        raw_text_parts.append(sys.stdin.read())

    # Check for session reference: a single arg like #last or #3
    session_resume = False
    if targets and len(targets) == 1 and targets[0].startswith("#"):
        resolved = _resolve_session_ref(targets[0])
        if resolved is None:
            sessions = _load_sessions()
            raise click.UsageError(
                f"No session found for {targets[0]!r}. "
                f"Available: #last or #1..#{len(sessions)}" if sessions else
                f"No saved sessions found."
            )
        click.echo(f"Resuming session {targets[0]!r}...", err=True)
        raw_text_parts.append(resolved)
        session_resume = True
    elif targets:
        raw_text_parts.append("\n".join(targets))

    if assigned:
        if targets:
            raise click.UsageError("Do not pass explicit TARGETS together with --assigned.")
        if g is None:
            raise click.UsageError(
                "No GitHub token available. --assigned requires authentication. "
                "Run `ghelper auth` or export GITHUB_TOKEN=<token>."
            )
        click.echo("Collecting assigned PRs for run shortcut...", err=True)
        try:
            assigned_title, assigned_entries, assigned_metadata = _collect_assigned_pr_entries(
                g,
                repo_opt=repo_opt,
                include_closed=include_closed,
                include_drafts=include_drafts,
                filter_pattern=filter_pattern,
            )
        except GithubException as exc:
            raise click.ClickException(
                f"GitHub API rejected the request while fetching assigned PRs "
                f"({_exc_message(exc)}). Check that your token is valid and has Actions:read scope."
            )
        if not assigned_entries:
            click.echo("No assigned PRs matched -- nothing to watch.")
            return
        click.echo(
            f"Using {len(assigned_entries)} assigned PR(s) from '{assigned_title}'.",
            err=True,
        )
        raw_text_parts.append(
            _format_markdown_summary(assigned_title, assigned_entries, assigned_metadata)
        )

    # No interactive prompt: when no args and no pipe, drop straight into the
    # dashboard with an empty tracker list. The user can add trackers from the
    # TUI (a) or via web UI / RPC.

    combined = "\n".join(raw_text_parts)

    # Save this as a new session (unless we are already resuming one)
    if not session_resume and combined.strip():
        session_idx = _save_session(combined, {})
        click.echo(f"  Session #{session_idx} saved — resume with: ghelper watch #{session_idx}", err=True)

    parsed = _parse_summary(combined)

    if parsed.title:
        click.echo(f"  {parsed.title}", err=True)
    if parsed.missing:
        click.echo(
            f"  {len(parsed.missing)} branch(es) without a backport PR yet:",
            err=True,
        )
        for entry in parsed.missing:
            click.echo(f"    [{entry.status.upper()}] {entry.branch}", err=True)

    # Merge ignore_ci from CLI option + summary header
    cli_ignore = [j.strip() for j in ignore_ci if j.strip()]
    effective_ignore_ci = list(dict.fromkeys(parsed.ignore_ci + cli_ignore))  # dedup, ordered
    if effective_ignore_ci:
        click.echo(f"  Ignoring CI jobs matching: {', '.join(effective_ignore_ci)}")

    interactive_tty = bool(not no_tui and sys.stdout.isatty())
    if not parsed.entries and not (interactive_tty or serve):
        raise click.UsageError(
            "No targets found. Pass URLs / run IDs, or pipe backport-tracker output."
        )

    # --- Pre-filter entries by status hint ---
    active_entries: list[SummaryEntry] = []
    for entry in parsed.entries:
        if entry.status in _SKIP_STATUSES:
            click.echo(f"  skipping [{entry.status.upper()}] {entry.url}")
        elif entry.status in _WARN_STATUSES:
            click.echo(
                f"  warning: [{entry.status.upper()}] {entry.url} — "
                "CI data not yet loaded; will watch anyway"
            )
            active_entries.append(entry)
        else:
            active_entries.append(entry)

    if not active_entries and not (interactive_tty or serve):
        click.echo("Nothing to watch — all entries were skipped.")
        return

    if active_entries and g is None:
        raise click.UsageError(
            "No GitHub token available. Resolving targets requires authentication. "
            "Run `ghelper auth`, export GITHUB_TOKEN=<token>, or start with --serve and log in from /auth."
        )

    # -----------------------------------------------------------------------
    # Resolve remaining targets → WorkflowRun objects
    # -----------------------------------------------------------------------
    all_runs: list[WorkflowRun] = []
    target_state: dict[str, dict] = {}
    run_ignore_ci: dict[int, list[str]] = {}

    for entry in active_entries:
        t = entry.url
        repo_name = _target_repo(t, repo_opt)
        repo_rules = _repo_config(user_cfg, repo_name) if repo_name else _DEFAULT_REPO_CONFIG
        target_state.setdefault(
            t,
            {
                "source": t,
                "status_hint": entry.status or "unknown",
                "run_ids": [],
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
            },
        )

        url_match = _URL_RE.search(t)
        pr_num = _target_pr_number(t)
        pr_obj: Any = None
        requirements_reason = ""
        if url_match and url_match.group("kind") == "pull" and repo_name:
            required_labels = repo_rules.get("required_labels", [])
            required_reviews = int(repo_rules.get("required_reviews", 0) or 0)
            if required_labels or required_reviews > 0:
                try:
                    pr_obj = g.get_repo(repo_name).get_pull(int(url_match.group("num")))
                except GithubException as exc:
                    raise click.ClickException(f"Cannot load PR for requirements check {t!r}: {_exc_message(exc)}")
                ok, reason = _pr_requirements_status(pr_obj, repo_rules)
                if not ok:
                    # Requirements are advisory for rerun gating. We still inspect CI state
                    # so failing checks can be rerun even before review/label gates are met.
                    requirements_reason = reason
                    click.echo(f"  note [REQUIREMENTS] {t} — {reason}; continuing CI evaluation")

        if pr_num is not None and repo_name:
            cached = _get_cached_pr_status(pr_status_cache, repo_name, pr_num)
            if cached is not None and cached.get("title"):
                click.echo(f"  {_short_target(t)} — using cached PR metadata", err=True)
                meta = _build_pr_display_meta(cached.get("title", ""), cached.get("branch", ""))
                if meta.get("is_backport") and not str(meta.get("backport_target", "") or "").strip():
                    meta["backport_target"] = str(cached.get("backport_target", "") or "").strip()
                meta["backport_source_pr"] = int(cached.get("source_pr") or 0)
                target_state[t].update(meta)
            else:
                click.echo(f"  {_short_target(t)} — fetching PR metadata...", err=True)
                try:
                    if pr_obj is None:
                        pr_obj = g.get_repo(repo_name).get_pull(pr_num)
                    title = str(getattr(pr_obj, "title", "")).strip()
                    branch = str(getattr(getattr(pr_obj, "head", None), "ref", "") or "")
                    base_branch = str(getattr(getattr(pr_obj, "base", None), "ref", "") or "")
                    body = str(getattr(pr_obj, "body", "") or "")
                    meta = _build_pr_display_meta(title, branch, body)
                    if meta.get("is_backport") and not str(meta.get("backport_target", "") or "").strip() and base_branch:
                        meta["backport_target"] = base_branch
                    target_state[t].update(meta)
                    is_merged = bool(getattr(pr_obj, "merged", False))
                    _set_cached_pr_status(
                        pr_status_cache,
                        repo_name,
                        pr_num,
                        branch=branch,
                        detail=str(entry.status or "unknown"),
                        title=title,
                        source_pr=meta.get("backport_source_pr", 0),
                        is_merged=is_merged,
                        backport_target=str(meta.get("backport_target", "") or ""),
                    )
                    pr_cache_changed = True
                except GithubException:
                    pass

        try:
            resolved = _resolve_target(t, repo_opt, g)
        except GithubException as exc:
            raise click.ClickException(f"Cannot resolve {t!r}: {_exc_message(exc)}")
        if not resolved:
            click.echo(f"  skipping [SUCCESS] {t} — all CI runs already passed")
            continue

        if requirements_reason:
            click.echo(f"  info [REQUIREMENTS] {_short_target(t)} — rerun allowed despite unmet requirements")

        all_runs.extend(resolved)
        for r in resolved:
            target_state[t]["run_ids"].append(r.id)
            merged_ignore = list(dict.fromkeys(
                effective_ignore_ci + list(repo_rules.get("ignore_ci", []))
            ))
            run_ignore_ci[r.id] = merged_ignore
            click.echo(f"  + {r.html_url}")

    target_state = {k: v for k, v in target_state.items() if v["run_ids"]}

    if not all_runs and not (interactive_tty or serve):
        raise click.ClickException("No workflow runs found for the given targets.")

    if pr_cache_changed:
        _save_pr_status_cache(pr_status_cache)
        click.echo(f"  cache: saved to {_PR_STATUS_CACHE_PATH}", err=True)

    # -----------------------------------------------------------------------
    # Boot the JSON-RPC server (canonical source of trackers/reruns).
    # The CLI's TUI observes and dispatches; retries happen server-side.
    # -----------------------------------------------------------------------
    import threading as _threading
    from ghelper.server import JSONRPCServer, create_app
    from aiohttp import web as _aiohttp_web

    _rpc_server = JSONRPCServer(token=token)
    # Seed trackers from every CLI-resolved target so they're managed identically
    # to web-UI/RPC-added ones. auto_rerun=True hands retry duty to the server.
    for _t_url in list(target_state.keys()):
        try:
            _tracker_dict = _rpc_server.submit_tracker_sync(
                target=_t_url,
                attempts=max_retries,
                interval_seconds=interval,
                auto_rerun=True,
                ignore_jobs=effective_ignore_ci,
            )
            target_state[_t_url]["tracker_id"] = int(_tracker_dict.get("id", 0))
        except ValueError as _exc:
            click.echo(f"  tracker skipped {_t_url}: {_exc}", err=True)
    _rpc_server.force_refresh_sync()

    _server_loop = asyncio.new_event_loop()
    _server_ready = _threading.Event()

    async def _server_runner() -> None:
        await _rpc_server.start_background_tasks()
        if serve:
            app = create_app(_rpc_server)
            runner_obj = _aiohttp_web.AppRunner(app)
            await runner_obj.setup()
            site = _aiohttp_web.TCPSite(runner_obj, host, port)
            await site.start()
            _rpc_server._record_event(f"HTTP listening on http://{host}:{port}")
            click.echo(f"  HTTP/JSON-RPC + web UI: http://{host}:{port}/", err=True)
        _server_ready.set()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await _rpc_server.stop_background_tasks()

    def _server_thread_main() -> None:
        asyncio.set_event_loop(_server_loop)
        try:
            _server_loop.run_until_complete(_server_runner())
        finally:
            _server_loop.close()

    _server_thread = _threading.Thread(
        target=_server_thread_main,
        name="ghelper-server",
        daemon=True,
    )
    _server_thread.start()
    _server_ready.wait(timeout=5)

    # Keep a fixed terminal window when interactive; in non-tty contexts,
    # retain the previous streaming behavior.
    use_rolling = bool(not no_tui and not quiet and sys.stdout.isatty())
    use_rich_tui = bool(not no_tui and sys.stdout.isatty() and _RICH_AVAILABLE)

    if not use_rich_tui and not quiet:
        click.echo(
            f"\nWatching {len(all_runs)} run(s) across {len(target_state)} target(s) | "
            f"retries={max_retries} | interval={interval}s"
        )
        if not no_tui and not _RICH_AVAILABLE:
            click.echo("Tip: install 'rich' for an enhanced TUI dashboard.")
        click.echo()

    # -----------------------------------------------------------------------
    # Per-run mutable state
    # -----------------------------------------------------------------------
    state: dict[int, dict] = {
        r.id: {
            "run": r,
            "repo_name": r.repository.full_name,
            "retries": 0,
            "done": False,
            "result": "pending",
            "last_status": "queued",
            "last_conclusion": None,
            "failed_jobs": [],
            "failed_jobs_conclusion": None,
        }
        for r in all_runs
    }
    repo_cache: dict[str, Any] = {}
    run_to_target: dict[int, str] = {}
    for t, t_state in target_state.items():
        for run_id in t_state["run_ids"]:
            run_to_target[run_id] = t

    # Keep a generous backlog so logs can be scrolled to any line in the dashboard.
    events: deque[str] = deque(maxlen=5000)
    ui_state: dict[str, Any] = {
        "focus": "targets",
        "offset_targets": 0,
        "offset_jobs": 0,
        "offset_logs": 0,
        "selected_targets": 0,
        "page_size_targets": 10,
        "page_size_jobs": 10,
        "page_size_logs": 10,
        "expanded_targets": set(),  # group keys with member sub-rows shown
        "tick": 0,                  # incremented every render, drives marquee scrolling
        "show_logs_pane": True,
        "show_log_excerpt": False,  # show grepped failed-CI log under each failed run
        # Active modal overlay (e.g. {"kind": "add_tracker", "buffer": "", "error": ""}).
        "modal": None,
    }

    def _apply_gh_token(raw_token: str, gh: Any, login: str) -> None:
        """Apply a validated GitHub token (shared by PAT modal and device flow)."""
        nonlocal g, token
        token = raw_token
        g = gh
        os.environ["GITHUB_TOKEN"] = raw_token
        _rpc_server.token = raw_token
        _rpc_server._gh = gh
        _rpc_server.user_login = login
        try:
            cfg = _load_user_config()
            cfg["token"] = raw_token
            _save_user_config(cfg)
        except Exception:
            pass
        _event(f"authenticated as {login}")

    def _submit_auth_modal(modal: dict[str, Any]) -> None:
        """Validate and apply a token entered from the TUI login modal."""
        raw_token = str(modal.get("buffer", "")).strip()
        if not raw_token:
            modal["error"] = "Token is required"
            return
        try:
            gh = Github(raw_token)
            login = gh.get_user().login
        except GithubException as exc:
            modal["error"] = f"Token rejected: {_exc_message(exc)}"
            return
        except Exception as exc:
            modal["error"] = f"Validation failed: {exc}"
            return

        _apply_gh_token(raw_token, gh, login)
        ui_state["modal"] = None

    def _start_device_login() -> None:
        """Initiate GitHub device authorization flow from the TUI (background thread)."""
        modal: dict[str, Any] = {
            "kind": "device_login",
            "phase": "starting",   # starting | waiting | error
            "url": "",
            "user_code": "",
            "error": "",
        }
        ui_state["modal"] = modal

        def _run() -> None:
            try:
                client_id = _device_flow_client_id()
            except Exception as exc:
                if ui_state.get("modal") is modal:
                    modal["phase"] = "error"
                    modal["error"] = str(exc)
                return
            try:
                device_data = _request_device_code(client_id)
            except Exception as exc:
                if ui_state.get("modal") is modal:
                    modal["phase"] = "error"
                    modal["error"] = str(exc)
                return

            url = str(device_data.get("verification_uri_complete") or device_data.get("verification_uri") or "")
            user_code = str(device_data.get("user_code") or "")
            if ui_state.get("modal") is modal:
                modal["url"] = url
                modal["user_code"] = user_code
                modal["phase"] = "waiting"

            try:
                raw_token = _poll_device_token(
                    client_id,
                    str(device_data.get("device_code") or ""),
                    int(device_data.get("interval") or 5),
                    int(device_data.get("expires_in") or 900),
                )
            except click.ClickException as exc:
                if ui_state.get("modal") is modal:
                    modal["phase"] = "error"
                    modal["error"] = exc.format_message()
                return
            except Exception as exc:
                if ui_state.get("modal") is modal:
                    modal["phase"] = "error"
                    modal["error"] = str(exc)
                return

            # User pressed Escape while polling — don't clobber state.
            if ui_state.get("modal") is not modal:
                return

            try:
                gh = Github(raw_token)
                login = gh.get_user().login
            except Exception as exc:
                modal["phase"] = "error"
                modal["error"] = f"Token validation failed: {exc}"
                return

            _apply_gh_token(raw_token, gh, login)
            if ui_state.get("modal") is modal:
                ui_state["modal"] = None

        t = _threading.Thread(target=_run, name="ghelper-device-login", daemon=True)
        t.start()

    def _event(msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        events.append(f"[{ts}] {msg}")
        # Mirror into the shared server log so the web UI sees the same activity.
        try:
            _rpc_server._record_event(msg)
        except Exception:
            pass
        # When the browser/web UI is available, keep terminal output quiet so
        # the on-screen log pane is the canonical view. Headless runs still
        # stream directly to the CLI.
        if not use_rolling and (not serve or no_tui):
            click.echo(f"  {msg}")

    def _target_group_key(t_url: str) -> str:
        """Aggregation key: stripped title. Falls back to URL so untitled rows stay solo."""
        title = str(target_state.get(t_url, {}).get("pr_base_title", "")).strip()
        if not title:
            return t_url
        # Re-apply conventional-commit stripping to handle nested prefixes like
        # "chore(backport): feat(ci): foo" — pr_base_title only peels one layer.
        for _ in range(3):
            cleaned = _clean_pr_title(title) or title
            if cleaned == title:
                break
            title = cleaned
        # Drop a trailing branch suffix that some backport scripts append, e.g. "(release-3.10)".
        title = re.sub(r"\s*\(?(?:backport|release)[-/ ][\w./-]+\)?$", "", title, flags=re.IGNORECASE).strip()
        return title.lower() or t_url

    def _target_groups() -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for t_url in target_state:
            groups.setdefault(_target_group_key(t_url), []).append(t_url)
        return groups

    def _group_primary(members: list[str]) -> str:
        """Representative URL — prefer the original (non-backport), else the first member."""
        for m in members:
            if not target_state.get(m, {}).get("is_backport", False):
                return m
        return members[0]

    def _group_members_for(t_url: str) -> list[str]:
        key = _target_group_key(t_url)
        return _target_groups().get(key, [t_url])

    def _target_totals(t_url: str) -> tuple[int, int, int]:
        total = done = ok = 0
        for member in _group_members_for(t_url):
            run_ids = target_state[member]["run_ids"]
            total += len(run_ids)
            done += sum(1 for rid in run_ids if state[rid]["done"])
            ok += sum(1 for rid in run_ids if state[rid]["result"] == "success")
        return total, done, ok

    def _target_label(t_url: str) -> str:
        total, done, ok = _target_totals(t_url)
        failed = done - ok
        if done < total:
            stage = "RUNNING"
        elif failed == 0:
            stage = "SUCCESS"
        else:
            stage = f"FAILED({failed})"
        return f"{stage}\n{ok}/{total} ok"

    def _group_branch_summary(members: list[str]) -> str:
        """Comma-separated list of distinct branches across the group's members."""
        branches: list[str] = []
        seen: set[str] = set()
        for m in members:
            t_state = target_state.get(m, {})
            if t_state.get("is_backport"):
                br = str(t_state.get("backport_target", "")).strip() or "unknown"
            else:
                br = "main"
            if br not in seen:
                seen.add(br)
                branches.append(br)
        return ", ".join(branches)

    def _target_title_subtitle(t_url: str) -> str:
        """Plain-text title line for rolling/click output (uses group's primary title)."""
        members = _group_members_for(t_url)
        primary = _group_primary(members)
        t_state = target_state.get(primary, {})
        base = str(t_state.get("pr_title", "")).strip()
        branches = _group_branch_summary(members)
        if len(members) > 1:
            return f"{base or 'PR title unavailable'}  \u2192 {branches}"
        if t_state.get("is_backport"):
            source = int(t_state.get("backport_source_pr") or 0)
            source_part = f"#{source} " if source else ""
            suffix = f"  \u21c6 backport {source_part}\u2192 {branches}"
            return f"{base}{suffix}" if base else suffix.strip()
        return base or "PR title unavailable"

    def _target_secondary_label(t_url: str) -> str:
        """Secondary identifier \u2014 for groups, a count; for solos, the URL/PR ref."""
        members = _group_members_for(t_url)
        if len(members) > 1:
            return f"{len(members)} PRs aggregated"
        return _short_target(t_url)

    def _target_rich_cell(t_url: str, prefix: str) -> Any:
        """Build a Rich Text cell for the target table row."""
        secondary = _target_secondary_label(t_url)
        primary = _target_title_subtitle(t_url)
        if _RichText is None:
            return f"{prefix} {primary}\n  {secondary}"
        cell = _RichText()
        cell.append(f"{prefix} {primary}")
        cell.append(f"\n  {secondary}", style="dim")
        return cell

    def _marquee(text: str, width: int) -> str:
        """Horizontal marquee that cycles `text` based on ui_state["tick"]."""
        if width <= 0 or len(text) <= width:
            return text
        padded = text + "   "
        offset = ui_state["tick"] % len(padded)
        rolled = padded[offset:] + padded[:offset]
        return rolled[:width]

    def _target_subtitle(primary_url: str, members: list[str], width: int) -> str:
        """Subtitle showing aggregated branches; marquees when too long."""
        branches = _group_branch_summary(members)
        t_state = target_state.get(primary_url, {})
        if len(members) > 1:
            full = f"→ {branches}"
        elif t_state.get("is_backport"):
            source = int(t_state.get("backport_source_pr") or 0)
            source_part = f"#{source} " if source else ""
            full = f"⇆ backport {source_part}→ {branches}"
        else:
            full = _short_target(primary_url)
        return _marquee(full, width) if width and len(full) > width else full

    def _sorted_target_items() -> list[str]:
        """Return one representative t_url per aggregation group, sorted by display title."""
        primaries = [_group_primary(members) for members in _target_groups().values()]
        return sorted(
            primaries,
            key=lambda t_url: (
                str(target_state[t_url].get("pr_base_title", "")).lower() or _short_target(t_url),
                _short_target(t_url),
            ),
        )

    def _target_row_items() -> list[tuple[str, str, str]]:
        """Flat row list expanding aggregated target groups.

        Each row is ("group", primary_url, primary_url) or
        ("member", primary_url, member_url) when its group is expanded.
        """
        rows: list[tuple[str, str, str]] = []
        for primary in _sorted_target_items():
            rows.append(("group", primary, primary))
            key = _target_group_key(primary)
            if key in ui_state["expanded_targets"]:
                members = sorted(
                    _group_members_for(primary),
                    key=lambda m: (
                        bool(target_state[m].get("is_backport", False)),
                        str(target_state[m].get("backport_target", "")).lower(),
                        _short_target(m),
                    ),
                )
                for m in members:
                    if m == primary:
                        continue
                    rows.append(("member", primary, m))
        return rows

    def _pr_status_items() -> list[str]:
        """Status detail for the currently selected PR (targets pane).

        Lists every workflow run attached to the selected target. Failed,
        running, and not-retryable runs are surfaced individually with
        their failed-job names; success/neutral/skipped/ignored runs are
        rolled up into a single summary line per category by default. Press
        space while focused on the jobs pane to expand the folded buckets.
        """
        rows = _target_row_items()
        if not rows:
            return ["(no PRs tracked yet)"]
        idx = max(0, min(ui_state.get("selected_targets", 0), len(rows) - 1))
        kind, primary, member_or_primary = rows[idx]
        if kind == "member":
            members = [member_or_primary]
            header_pr = member_or_primary
        else:
            members = _group_members_for(primary)
            header_pr = primary

        t_state = target_state.get(header_pr, {})
        title = str(t_state.get("pr_title", "")).strip() or "(untitled PR)"
        if len(members) > 1:
            header = f"PR · {title}  ({len(members)} aggregated)"
        else:
            header = f"PR · {title}  {_short_target(header_pr)}"

        # Collect all run_ids attached to this PR group.
        all_run_ids: list[int] = []
        for m in members:
            for rid in target_state.get(m, {}).get("run_ids", []):
                if rid in state:
                    all_run_ids.append(rid)

        if not all_run_ids:
            return [header, "  (no workflow runs yet — waiting for the next poll)"]

        # Bucket runs by outcome category.
        buckets: dict[str, list[int]] = {
            "running": [],
            "failed": [],
            "not_retryable": [],
            "passed": [],   # success / neutral / skipped
            "ignored": [],
        }
        for rid in all_run_ids:
            s = state[rid]
            result = s.get("result", "pending")
            status = s.get("last_status", "")
            if result == "ignored":
                buckets["ignored"].append(rid)
            elif status != "completed":
                buckets["running"].append(rid)
            elif result == "failed":
                buckets["failed"].append(rid)
            elif result == "not_retryable":
                buckets["not_retryable"].append(rid)
            else:
                buckets["passed"].append(rid)

        show_passed = bool(ui_state.get("jobs_show_passed", False))
        show_ignored = bool(ui_state.get("jobs_show_ignored", False))

        def _wf_name(rid: int) -> str:
            run = state[rid].get("run")
            name = str(getattr(run, "name", "") or "").strip()
            return name or f"run-{rid}"

        def _run_line(rid: int, marker: str) -> str:
            s = state[rid]
            wf = _wf_name(rid)
            conc = s.get("last_conclusion") or s.get("last_status") or "-"
            return f"  {marker} {wf}  ({s['repo_name']}#{rid} · {conc})"

        out: list[str] = [header]

        show_log_excerpt = bool(ui_state.get("show_log_excerpt", False))

        if buckets["failed"]:
            out.append(f"▾ failed ({len(buckets['failed'])})")
            for rid in sorted(buckets["failed"]):
                out.append(_run_line(rid, "✗"))
                for j in (state[rid].get("failed_jobs") or [])[:5]:
                    out.append(f"      • {j}")
                extra = len(state[rid].get("failed_jobs") or []) - 5
                if extra > 0:
                    out.append(f"      • … +{extra} more")
                if show_log_excerpt:
                    excerpt = state[rid].get("log_excerpt")
                    if excerpt is None:
                        out.append("      [log] press L to load excerpt")
                    elif excerpt.get("error"):
                        out.append(f"      [log] {excerpt['error']}")
                    else:
                        job_name = excerpt.get("job_name", "")
                        lines = excerpt.get("lines") or []
                        out.append(f"      [log] {job_name}:")
                        for line in lines[:15]:
                            out.append(f"        {line}")
                        if excerpt.get("truncated") or len(lines) > 15:
                            out.append("        … (truncated — use `ghelper logs` for full output)")

        if buckets["running"]:
            out.append(f"▾ running ({len(buckets['running'])})")
            for rid in sorted(buckets["running"]):
                out.append(_run_line(rid, "↻"))

        if buckets["not_retryable"]:
            out.append(f"▾ not-retryable ({len(buckets['not_retryable'])})")
            for rid in sorted(buckets["not_retryable"]):
                out.append(_run_line(rid, "!"))

        if buckets["passed"]:
            marker = "▾" if show_passed else "▸"
            out.append(f"{marker} passed ({len(buckets['passed'])})  — success/neutral/skipped")
            if show_passed:
                for rid in sorted(buckets["passed"]):
                    out.append(_run_line(rid, "✓"))

        if buckets["ignored"]:
            marker = "▾" if show_ignored else "▸"
            out.append(f"{marker} ignored ({len(buckets['ignored'])})")
            if show_ignored:
                for rid in sorted(buckets["ignored"]):
                    out.append(_run_line(rid, "·"))

        if len(out) == 1:
            out.append("  (no workflow runs in any bucket)")
        return out

    def _ensure_selected_visible(
        total: int,
        page_size: int,
        selected: int,
        offset: int,
    ) -> tuple[int, int]:
        if total <= 0:
            return 0, 0

        selected = max(0, min(selected, total - 1))
        offset = _clip_offset(total, page_size, offset)
        if selected < offset:
            offset = selected
        elif selected >= offset + max(1, page_size):
            offset = selected - max(1, page_size) + 1
        offset = _clip_offset(total, page_size, offset)
        return selected, offset

    def _clip_offset(total: int, page_size: int, offset: int) -> int:
        if total <= 0:
            return 0
        max_offset = max(0, total - max(1, page_size))
        return max(0, min(offset, max_offset))

    def _scroll_meta(total: int, page_size: int, offset: int) -> str:
        if total == 0:
            return "0/0"
        start = offset + 1
        end = min(total, offset + page_size)
        return f"{start}-{end}/{total}"

    def _pane_title(base: str, pane: str, total: int, page_size: int, offset: int) -> str:
        marker = "*" if ui_state["focus"] == pane else " "
        return f"{marker} {base} [{_scroll_meta(total, page_size, offset)}]"

    # Buffer for escape sequences that arrive split across reads.
    _key_buffer: dict[str, str] = {"pending": ""}

    # Maps full escape sequences to logical key names.
    _ESC_KEYS = {
        "\x1b[A": "up", "\x1bOA": "up",
        "\x1b[B": "down", "\x1bOB": "down",
        "\x1b[C": "right", "\x1bOC": "right",
        "\x1b[D": "left", "\x1bOD": "left",
        "\x1b[5~": "pageup",
        "\x1b[6~": "pagedown",
        "\x1b[H": "home", "\x1bOH": "home",
        "\x1b[F": "end", "\x1bOF": "end",
    }

    def _drain_escape_sequences(text: str) -> tuple[list[str], str]:
        keys: list[str] = []
        i = 0
        while i < len(text):
            ch = text[i]
            if ch != "\x1b":
                keys.append(ch)
                i += 1
                continue
            # Possible escape sequence — try to match against the table.
            matched: Optional[tuple[str, str]] = None
            for seq, name in _ESC_KEYS.items():
                if text.startswith(seq, i):
                    matched = (seq, name)
                    break
            if matched:
                keys.append(matched[1])
                i += len(matched[0])
                continue
            # Could be a partial sequence at the tail — buffer it for next read.
            tail = text[i:]
            if any(seq.startswith(tail) for seq in _ESC_KEYS):
                return keys, tail
            # Lone ESC press.
            keys.append("escape")
            i += 1
        return keys, ""

    def _read_keys_nonblocking() -> list[str]:
        if not sys.stdin.isatty() or termios is None:
            return []

        chunks: list[str] = [_key_buffer["pending"]] if _key_buffer["pending"] else []
        _key_buffer["pending"] = ""
        try:
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0)
                if not ready:
                    break
                data = os.read(sys.stdin.fileno(), 32)
                if not data:
                    break
                chunks.append(data.decode("utf-8", errors="ignore"))
        except (OSError, ValueError):
            return []

        if not chunks:
            return []
        keys, pending = _drain_escape_sequences("".join(chunks))
        _key_buffer["pending"] = pending
        return keys

    def _selectable_total(focus: str) -> int:
        if focus == "targets":
            return len(_target_row_items())
        return 0

    def _submit_modal_buffer(modal: dict[str, Any]) -> None:
        """Parse the modal buffer and add the resulting trackers.

        Buffer grammar (any of):
          * one or more PR URLs (separated by whitespace, commas, or newlines)
          * a Backport-Tracker / `ghelper ls` markdown summary
          * `#last` or `#N` to expand a saved session
          * `:assigned [REGEX]` to pull PRs assigned to the current user
        """
        if modal.get("kind") == "auth_token":
            _submit_auth_modal(modal)
            return

        buf = str(modal.get("buffer", "")).strip()
        if not buf:
            modal["error"] = "Provide URLs, a markdown summary, #N, or :assigned"
            return

        urls: list[str] = []
        try:
            if buf.startswith(":assigned"):
                if g is None:
                    modal["error"] = "Auth required for :assigned. Login first from /auth or restart with a token."
                    return
                _, _, rest = buf.partition(":assigned")
                filter_pat = rest.strip() or None
                _, entries, _ = _collect_assigned_pr_entries(
                    g,
                    repo_opt=repo_opt,
                    include_closed=False,
                    include_drafts=False,
                    filter_pattern=filter_pat,
                )
                urls = [e[1] for e in entries]
            elif buf.startswith("#"):
                resolved = _resolve_session_ref(buf.split()[0])
                if resolved is None:
                    modal["error"] = f"No saved session for {buf!r}"
                    return
                parsed = _parse_summary(resolved)
                urls = [e.url for e in parsed.entries]
            else:
                parsed = _parse_summary(buf)
                urls = [e.url for e in parsed.entries]
                if not urls:
                    urls = list(_extract_urls(buf))
        except Exception as exc:
            modal["error"] = f"input error: {exc}"
            return

        if not urls:
            modal["error"] = "No GitHub URLs found in input"
            return

        added = 0
        errors: list[str] = []
        for url in urls:
            try:
                _rpc_server.submit_tracker_sync(
                    target=url,
                    attempts=max_retries,
                    interval_seconds=interval,
                    auto_rerun=True,
                    ignore_jobs=effective_ignore_ci,
                )
                added += 1
            except ValueError as exc:
                errors.append(f"{url}: {exc}")
        if added:
            _event(f"added {added} tracker(s) from input")
        if errors:
            modal["error"] = "; ".join(errors[:3])
            return
        ui_state["modal"] = None

    def _handle_modal_keys(keys: list[str]) -> bool:
        """When a modal is open, route all keystrokes into its buffer."""
        modal = ui_state.get("modal")
        if not modal:
            return False
        # Device login modal: only Escape is meaningful (flow runs in background).
        if modal.get("kind") == "device_login":
            for key in keys:
                if key == "escape":
                    ui_state["modal"] = None
            return True
        # Confirm-update-branch modal: y confirms the action, n/Esc cancels.
        if modal.get("kind") == "confirm_update_branch":
            for key in keys:
                if key in {"y", "Y"}:
                    tid = int(modal.get("tracker_id") or 0)
                    label = str(modal.get("label", ""))
                    ui_state["modal"] = None
                    if tid:
                        result = _rpc_server.update_branch_tracker_sync(tid)
                        if result.get("ok"):
                            _event(f"update branch requested for {label}")
                        else:
                            _event(f"update branch failed for {label}: {result.get('error', 'error')}")
                    return True
                if key in {"n", "N", "escape"}:
                    ui_state["modal"] = None
                    return True
            return True
        changed = False
        submit = False
        for key in keys:
            if key == "escape":
                ui_state["modal"] = None
                return True
            if key in {"\r", "\n"}:
                if modal.get("kind") != "auth_token":
                    modal["buffer"] = str(modal.get("buffer", "")) + "\n"
                submit = True
                changed = True
            elif key == "\x7f":  # backspace
                modal["buffer"] = str(modal.get("buffer", ""))[:-1]
                changed = True
            elif len(key) == 1 and (key.isprintable() or key == " "):
                modal["buffer"] = str(modal.get("buffer", "")) + key
                changed = True
        if submit:
            _submit_modal_buffer(modal)
        return changed

    def _handle_ui_keys() -> bool:
        keys = _read_keys_nonblocking()
        if ui_state.get("modal"):
            return _handle_modal_keys(keys)
        changed = False
        for key in keys:
            if key == "a":
                ui_state["modal"] = {"kind": "add_tracker", "buffer": "", "error": ""}
                changed = True
                continue

            if key == "i":
                ui_state["modal"] = {"kind": "auth_token", "buffer": "", "error": ""}
                changed = True
                continue

            if key == "w":
                _start_device_login()
                changed = True
                continue

            if key == "d" and ui_state["focus"] == "targets":
                rows = _target_row_items()
                if rows:
                    idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                    _, primary, _ = rows[idx]
                    tid = int(target_state.get(primary, {}).get("tracker_id") or 0)
                    if tid and _rpc_server.remove_tracker_sync(tid):
                        _event(f"removed tracker {primary}")
                changed = True
                continue

            if key == "b" and ui_state["focus"] == "targets":
                rows = _target_row_items()
                if rows:
                    idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                    _, primary, _ = rows[idx]
                    tid = int(target_state.get(primary, {}).get("tracker_id") or 0)
                    if tid:
                        # Mutating action — gate behind a confirm modal rather
                        # than acting on the bare keystroke.
                        ui_state["modal"] = {
                            "kind": "confirm_update_branch",
                            "tracker_id": tid,
                            "label": primary,
                            "error": "",
                        }
                changed = True
                continue

            if key == "r":
                if ui_state["focus"] == "targets":
                    rows = _target_row_items()
                    if rows:
                        idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                        _, primary, _ = rows[idx]
                        tid = int(target_state.get(primary, {}).get("tracker_id") or 0)
                        if tid:
                            _rpc_server.force_refresh_sync(tid)
                            _event(f"refresh requested for {primary}")
                else:
                    _rpc_server.force_refresh_sync()
                    _event("refresh requested for all trackers")
                changed = True
                continue

            if key == "\t":
                # Tab cycles PR list → PR-status (jobs) → logs → back.
                order: list[str] = ["targets", "jobs"]
                if ui_state.get("show_logs_pane", True):
                    order.append("logs")
                idx = order.index(ui_state["focus"]) if ui_state["focus"] in order else 0
                ui_state["focus"] = order[(idx + 1) % len(order)]
                changed = True
                continue

            if key == "l":
                ui_state["show_logs_pane"] = not ui_state.get("show_logs_pane", True)
                if not ui_state["show_logs_pane"] and ui_state["focus"] == "logs":
                    ui_state["focus"] = "targets"
                changed = True
                continue

            if key == "L":
                # Toggle the failed-CI log excerpt; lazily download for the
                # selected PR's failed runs when switching it on.
                ui_state["show_log_excerpt"] = not ui_state.get("show_log_excerpt", False)
                if ui_state["show_log_excerpt"]:
                    rows = _target_row_items()
                    if rows:
                        idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                        kind, primary, member = rows[idx]
                        members = [member] if kind == "member" else _group_members_for(primary)
                        for m in members:
                            tid = int(target_state.get(m, {}).get("tracker_id") or 0)
                            if not tid:
                                continue
                            for rid in target_state.get(m, {}).get("run_ids", []):
                                s = state.get(rid)
                                if not s or s.get("result") != "failed":
                                    continue
                                if s.get("log_excerpt") is not None:
                                    continue
                                _event(f"loading log excerpt for run {rid}…")
                                try:
                                    s["log_excerpt"] = _rpc_server.log_excerpt_sync(tid, rid)
                                except Exception as exc:  # pragma: no cover - defensive
                                    s["log_excerpt"] = {"error": str(exc), "lines": []}
                changed = True
                continue

            if key == "E":
                # Defer to the main loop so it can pause the Live display and
                # restore the terminal before launching the editor.
                rows = _target_row_items()
                if rows:
                    idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                    _, primary, _ = rows[idx]
                    repo = _target_repo(primary, repo_opt) or ""
                    if repo:
                        ui_state["pending_edit_repo"] = repo
                changed = True
                continue

            focus = ui_state["focus"]
            selectable = focus == "targets"

            if key in {"j", "down"}:
                if selectable:
                    sel_key = f"selected_{focus}"
                    ui_state[sel_key] = min(ui_state[sel_key] + 1, max(0, _selectable_total(focus) - 1))
                else:
                    ui_state[f"offset_{focus}"] += 1
                changed = True
                continue

            if key in {"k", "up"}:
                if selectable:
                    sel_key = f"selected_{focus}"
                    ui_state[sel_key] = max(0, ui_state[sel_key] - 1)
                else:
                    ui_state[f"offset_{focus}"] = max(0, ui_state[f"offset_{focus}"] - 1)
                changed = True
                continue

            if key in {"pagedown", "right"}:
                page = max(1, int(ui_state.get(f"page_size_{focus}", 10) or 10))
                if selectable:
                    ui_state[f"selected_{focus}"] = min(
                        ui_state[f"selected_{focus}"] + page,
                        max(0, _selectable_total(focus) - 1),
                    )
                else:
                    ui_state[f"offset_{focus}"] += page
                changed = True
                continue

            if key in {"pageup", "left"}:
                page = max(1, int(ui_state.get(f"page_size_{focus}", 10) or 10))
                if selectable:
                    ui_state[f"selected_{focus}"] = max(0, ui_state[f"selected_{focus}"] - page)
                else:
                    ui_state[f"offset_{focus}"] = max(0, ui_state[f"offset_{focus}"] - page)
                changed = True
                continue

            if key in {"g", "home"}:
                if selectable:
                    ui_state[f"selected_{focus}"] = 0
                else:
                    ui_state[f"offset_{focus}"] = 0
                changed = True
                continue

            if key in {"G", "end"}:
                if selectable:
                    ui_state[f"selected_{focus}"] = max(0, _selectable_total(focus) - 1)
                else:
                    ui_state[f"offset_{focus}"] = 10**9
                changed = True
                continue

            if key in {" ", "x"}:
                if focus == "targets":
                    rows = _target_row_items()
                    if rows:
                        idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                        _, primary, _ = rows[idx]
                        key_id = _target_group_key(primary)
                        if key_id in ui_state["expanded_targets"]:
                            ui_state["expanded_targets"].discard(key_id)
                        elif len(_group_members_for(primary)) > 1:
                            ui_state["expanded_targets"].add(key_id)
                        changed = True
                elif focus == "jobs":
                    ui_state["jobs_show_passed"] = not ui_state.get("jobs_show_passed", False)
                    changed = True
                continue

            if key in {"o", "\r", "\n"}:
                if focus == "targets":
                    rows = _target_row_items()
                    if rows:
                        idx = max(0, min(ui_state["selected_targets"], len(rows) - 1))
                        kind, primary, member_or_primary = rows[idx]
                        if kind == "member":
                            webbrowser.open_new_tab(member_or_primary)
                            _event(f"Opened {member_or_primary}")
                        else:
                            members = _group_members_for(primary)
                            for m in members:
                                webbrowser.open_new_tab(m)
                            if len(members) > 1:
                                _event(f"Opened {len(members)} PRs: {', '.join(members)}")
                            else:
                                _event(f"Opened target in browser: {members[0]}")
                        changed = True
                continue
        return changed

    def _render_dashboard() -> None:
        click.clear()
        click.echo("ghelper live dashboard")
        click.echo(
            f"Targets={len(target_state)} | Runs={len(state)} | "
            f"max-retries={max_retries} | interval={interval}s"
        )
        if shutdown_requested:
            click.echo("Status: stopping (Ctrl-C received)")
        click.echo()
        click.echo("Target totals:")
        for t in _sorted_target_items():
            hint = target_state[t]["status_hint"].upper()
            click.echo(f"  {_target_title_subtitle(t)} [{hint}] -> {_target_label(t)}")
            click.echo(f"    {_target_secondary_label(t)}")

        click.echo()
        click.echo("Run states:")
        for run_id in sorted(state):
            s = state[run_id]
            label = f"{s['repo_name']}#{run_id}"
            live = f"{s['last_status']}/{s['last_conclusion'] or '-'}"
            run_target = run_to_target.get(run_id, "")
            pr_title = _target_title_subtitle(run_target) if run_target else "PR title unavailable"
            click.echo(
                f"  {label} | PR: {pr_title} | {s['result']} | retries {s['retries']}/{max_retries} | {live}"
            )

        click.echo()
        click.echo("Recent logs:")
        if events:
            for e in events:
                click.echo(f"  {e}")
        else:
            click.echo("  (no events yet)")
        click.echo()
        click.echo("Ctrl-C to stop")

    def _build_summary_lines() -> list[str]:
        success_count = sum(1 for s in state.values() if s["result"] == "success")
        failed = [s for s in state.values() if s["result"] in {"failed", "api_error", "not_retryable"}]
        lines = [f"Final: {success_count}/{len(state)} succeeded, {len(failed)} need attention."]
        for s in failed:
            target = run_to_target.get(s["run"].id, "")
            lines.append(f"  ✗ {_short_target(target)} → {s['run'].html_url} ({s['result']})")
        return lines

    def _build_rich_dashboard() -> Any:
        if not _RICH_AVAILABLE or _RichTable is None or _RichPanel is None or _RichGroup is None:
            return "Rich TUI unavailable"

        ui_state["tick"] = int(ui_state.get("tick", 0)) + 1

        all_done = all(s["done"] for s in state.values())
        status = "done" if (shutdown_requested and all_done) else ("stopping" if shutdown_requested else "running")

        try:
            term_size = os.get_terminal_size()
            term_width = term_size.columns
            term_height = term_size.lines
        except OSError:
            term_width = 80
            term_height = 24

        show_logs_pane = ui_state.get("show_logs_pane", True)

        # Layout sizing — every value below is in TERMINAL ROWS (not table items),
        # and we feed the same numbers to the Rich Layout as `size=` so the
        # page-size math matches what's actually visible on screen.
        footer_size = 5 if serve else 4
        top_spacer = 1
        # Each table renders with a 2-row border (top + bottom).
        TABLE_BORDER = 2
        available_rows = max(8, term_height - footer_size - top_spacer)

        # Logs: keep modest — a strip at the bottom, not a half-screen.
        # Roughly 1/4 of the available rows, clamped to 5..10.
        logs_outer = max(5, min(10, available_rows // 4)) if show_logs_pane else 0
        logs_inner = max(2, logs_outer - TABLE_BORDER) if show_logs_pane else 0

        # Remaining space goes to the primary pane (targets or jobs PR-status).
        targets_outer = max(8, available_rows - logs_outer)
        targets_rows = max(2, targets_outer - TABLE_BORDER)

        ui_state["page_size_targets"] = targets_rows
        ui_state["page_size_jobs"] = targets_rows
        ui_state["page_size_logs"] = max(1, logs_inner)

        if not show_logs_pane and ui_state["focus"] == "logs":
            ui_state["focus"] = "targets"

        target_rows_v = _target_row_items()
        log_items = list(events)

        ui_state["selected_targets"], ui_state["offset_targets"] = _ensure_selected_visible(
            len(target_rows_v), targets_rows, ui_state["selected_targets"], ui_state["offset_targets"],
        )
        ui_state["offset_logs"] = _clip_offset(len(log_items), logs_inner, ui_state["offset_logs"])

        visible_target_rows = target_rows_v[
            ui_state["offset_targets"]: ui_state["offset_targets"] + targets_rows
        ]
        visible_logs = log_items[
            ui_state["offset_logs"]: ui_state["offset_logs"] + logs_inner
        ]

        # ------- targets pane -------
        # Single-line rows: keeps `targets_rows` (page size) equal to actual
        # visible row count, which is what makes scrolling/pagination behave.
        target_table = _RichTable(expand=True, show_header=False, show_edge=True, pad_edge=False)
        target_table.add_column("Target / PR", style="cyan", overflow="ellipsis", no_wrap=True)
        target_table.add_column("State", style="green", justify="right", no_wrap=True)
        subtitle_width = max(20, term_width - 18)
        for row_index, (kind, primary, member_or_primary) in enumerate(visible_target_rows):
            absolute_index = ui_state["offset_targets"] + row_index
            selected = absolute_index == ui_state["selected_targets"] and ui_state["focus"] == "targets"
            prefix = ">" if selected else " "
            members = _group_members_for(primary)
            key_id = _target_group_key(primary)
            expanded = key_id in ui_state["expanded_targets"]

            if kind == "group":
                t_state = target_state.get(primary, {})
                base = str(t_state.get("pr_title", "")).strip() or "PR title unavailable"
                fold = "▾" if expanded else ("▸" if len(members) > 1 else " ")
                count = f" ({len(members)})" if len(members) > 1 else ""
                subtitle = _target_subtitle(primary, members, subtitle_width)
                if _RichText is not None:
                    cell: Any = _RichText()
                    cell.append(f"{prefix} {fold} {base}{count}  ")
                    cell.append(subtitle, style="dim")
                else:
                    cell = f"{prefix} {fold} {base}{count}  {subtitle}"
                state_text = _target_label(primary).replace("\n", " ")
            else:
                m_state = target_state.get(member_or_primary, {})
                br = str(m_state.get("backport_target", "")).strip() or "backport"
                short = _short_target(member_or_primary)
                if _RichText is not None:
                    cell = _RichText()
                    cell.append(f"{prefix}    └ {br}  ")
                    cell.append(short, style="dim")
                else:
                    cell = f"{prefix}    L {br}  {short}"
                run_ids = m_state.get("run_ids", [])
                m_total = len(run_ids)
                m_done = sum(1 for rid in run_ids if state[rid]["done"])
                m_ok = sum(1 for rid in run_ids if state[rid]["result"] == "success")
                m_failed = m_done - m_ok
                stage = "RUNNING" if m_done < m_total else ("SUCCESS" if m_failed == 0 else f"FAILED({m_failed})")
                state_text = f"{stage} {m_ok}/{m_total}"

            target_table.add_row(
                cell,
                state_text,
                style="bold white on blue" if selected else "",
            )
        # Fill any remaining space so the pane's border encloses the full slot.
        while target_table.row_count < targets_rows:
            target_table.add_row("", "")

        # ------- jobs pane (PR status detail; swaps into the targets slot when focused) -------
        jobs_table = _RichTable(expand=True, show_header=False, show_edge=True, pad_edge=False)
        jobs_table.add_column("Job", overflow="ellipsis", no_wrap=True)
        job_items = _pr_status_items()
        ui_state["offset_jobs"] = _clip_offset(len(job_items), targets_rows, ui_state["offset_jobs"])
        visible_job_items = job_items[ui_state["offset_jobs"]: ui_state["offset_jobs"] + targets_rows]
        if visible_job_items:
            for line in visible_job_items:
                jobs_table.add_row(line)
        else:
            jobs_table.add_row("(select a PR in the targets pane)")
        while jobs_table.row_count < targets_rows:
            jobs_table.add_row("")

        # ------- logs pane -------
        logs_table = _RichTable(expand=True, show_header=False, show_edge=True, pad_edge=False)
        logs_table.add_column("Event", overflow="ellipsis", no_wrap=True)
        if visible_logs:
            for event in visible_logs:
                logs_table.add_row(event)
        else:
            logs_table.add_row("(no events yet)")
        while logs_table.row_count < logs_inner:
            logs_table.add_row("")

        # ------- footer (status surfaced via marquee/help line) -------
        if all_done or shutdown_requested:
            summary_lines = _build_summary_lines()
        else:
            summary_lines = []
        rolling_line = (" · ".join(summary_lines) if summary_lines else (log_items[-1] if log_items else "(awaiting events)"))
        rolling_line = _marquee(rolling_line, max(20, term_width - 4))

        focus_name = ui_state["focus"]
        if focus_name == "targets":
            scroll = _scroll_meta(len(target_rows_v), targets_rows, ui_state["offset_targets"])
        elif focus_name == "jobs":
            scroll = _scroll_meta(len(job_items), targets_rows, ui_state["offset_jobs"])
        else:
            scroll = _scroll_meta(len(log_items), logs_inner, ui_state["offset_logs"])
        if focus_name == "jobs":
            space_hint = "space toggle passed"
        else:
            space_hint = "space expand"
        help_text = (
            f"[{focus_name} {scroll} · {status}] TAB pane · j/k row · ←/→ page · {space_hint} · "
            f"a add · i login · w device-login · b update-branch · d remove · r refresh · l toggle logs · L ci-log · E edit-filter · o/Enter open · Ctrl-C exit"
        )
        footer_border = "blue" if status == "running" else (
            "green" if all(s["result"] == "success" for s in state.values()) else "red"
        )
        if serve:
            serve_line = f"[bold cyan]web UI[/bold cyan] http://{host}:{port}/  ·  [dim]POST /rpc[/dim]"
            footer_body = f"{serve_line}\n{help_text}\n{rolling_line}"
        else:
            footer_body = f"{help_text}\n{rolling_line}"
        footer_panel = _RichPanel(footer_body, border_style=footer_border)

        modal = ui_state.get("modal")
        if modal and modal.get("kind") == "add_tracker":
            buf = str(modal.get("buffer", ""))
            err = str(modal.get("error", "") or "")
            modal_body = (
                "Add trackers — paste URLs, a markdown summary, [bold]#last[/bold]/[bold]#N[/bold], or "
                "[bold]:assigned [REGEX][/bold]. Enter submits, Esc cancels.\n\n"
                f"[dim]›[/dim] {buf}_"
            )
            if err:
                modal_body += f"\n\n[red]{err}[/red]"
            modal_panel = _RichPanel(modal_body, title="Add tracker", border_style="yellow")
        elif modal and modal.get("kind") == "auth_token":
            buf = str(modal.get("buffer", ""))
            masked = "*" * len(buf)
            err = str(modal.get("error", "") or "")
            modal_body = (
                "Login — paste a GitHub token and press Enter to authenticate. Esc cancels.\n\n"
                f"[dim]token[/dim]: {masked}_"
            )
            if err:
                modal_body += f"\n\n[red]{err}[/red]"
            modal_panel = _RichPanel(modal_body, title="Login", border_style="cyan")
        elif modal and modal.get("kind") == "device_login":
            phase = modal.get("phase", "starting")
            if phase == "starting":
                modal_body = "Starting GitHub device authorization…"
            elif phase == "waiting":
                url = str(modal.get("url", ""))
                user_code = str(modal.get("user_code", ""))
                modal_body = (
                    "Open the URL below in a browser and enter the code shown.\n\n"
                    f"  [bold cyan]{url}[/bold cyan]\n\n"
                    f"  Code: [bold yellow]{user_code}[/bold yellow]\n\n"
                    "[dim]Waiting for authorization… Esc to cancel.[/dim]"
                )
            elif phase == "error":
                err = str(modal.get("error", "unknown error"))
                modal_body = f"[red]Device login failed:[/red] {err}\n\n[dim]Esc to dismiss[/dim]"
            else:
                modal_body = "…"
            modal_panel = _RichPanel(modal_body, title="Device Login", border_style="magenta")
        elif modal and modal.get("kind") == "confirm_update_branch":
            label = str(modal.get("label", ""))
            modal_body = (
                "Update branch for:\n\n"
                f"  [bold]{label}[/bold]\n\n"
                "This updates the PR branch with its base branch on GitHub.\n"
                "[bold]y[/bold] confirm · [bold]n[/bold]/Esc cancel"
            )
            modal_panel = _RichPanel(modal_body, title="Confirm update branch", border_style="yellow")
        else:
            modal_panel = None

        primary_pane = jobs_table if ui_state["focus"] == "jobs" else target_table

        # Build a Rich Layout so each pane fills its share of the terminal,
        # rather than collapsing to its natural row count when content is sparse.
        try:
            _rich_layout = importlib.import_module("rich.layout")
            _Layout = getattr(_rich_layout, "Layout", None)
        except (ImportError, AttributeError):
            _Layout = None

        if _Layout is None:
            # Fallback: stack as Group (loses fill-to-height, but renders).
            vertical: list[Any] = [primary_pane]
            if show_logs_pane:
                vertical.append(logs_table)
            if modal_panel is not None:
                vertical.append(modal_panel)
            vertical.append(footer_panel)
            return _RichGroup(*vertical)

        sections: list[Any] = []
        # Leave one row at the top: VSCode (and a few other terminals) overlay
        # the running command/process title on the first visible row of the
        # terminal, which clips whatever pane sits flush at the top.
        sections.append(_Layout("", name="PR CI Tracker", size=top_spacer))
        sections.append(_Layout(primary_pane, name="primary", size=targets_outer))
        if show_logs_pane:
            sections.append(_Layout(logs_table, name="logs", size=logs_outer))
        if modal_panel is not None:
            sections.append(_Layout(modal_panel, name="modal", size=10))
        sections.append(_Layout(footer_panel, name="footer", size=footer_size))

        root = _Layout()
        root.split_column(*sections)
        return root

    def _repo(name: str) -> Any:
        if name not in repo_cache:
            repo_cache[name] = g.get_repo(name)
        return repo_cache[name]

    # -----------------------------------------------------------------------
    # Polling loop
    # -----------------------------------------------------------------------
    live_obj: Any = None
    window_resized = False
    original_sigwinch: Any = None
    original_sigint: Any = None
    original_tty: Any = None
    shutdown_requested = False
    _announced_done: set[int] = set()

    def _on_window_resize(signum: int, frame: Any) -> None:
        """Signal handler for terminal window resize."""
        nonlocal window_resized
        window_resized = True

    def _on_sigint(signum: int, frame: Any) -> None:
        """SIGINT handler to integrate Ctrl-C with dashboard rendering."""
        nonlocal shutdown_requested
        if shutdown_requested:
            raise KeyboardInterrupt
        shutdown_requested = True
        _event("Interrupted by user (Ctrl-C) — shutting down...")

    # Set up signal handler for window resize (Unix/Linux/macOS)
    if hasattr(signal, "SIGWINCH"):
        original_sigwinch = signal.signal(signal.SIGWINCH, _on_window_resize)
    original_sigint = signal.signal(signal.SIGINT, _on_sigint)

    try:
        if use_rich_tui:
            if _RichLive is None:
                raise click.ClickException("Rich TUI requested but rich is not available.")
            if termios is not None and tty is not None and sys.stdin.isatty():
                original_tty = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            live_obj = _RichLive(_build_rich_dashboard(), refresh_per_second=4)
            live_obj.start()

        class _RunShim:
            """Minimal stand-in for a PyGithub WorkflowRun, derived from a tracker job dict."""
            __slots__ = ("id", "name", "status", "conclusion", "html_url")

            def __init__(self, job: dict[str, Any]) -> None:
                self.id = int(job.get("id", 0) or 0)
                self.name = str(job.get("name", "") or "")
                self.status = str(job.get("status", "") or "")
                self.conclusion = str(job.get("conclusion", "") or "") or None
                self.html_url = str(job.get("url", "") or "")

        def _derive_result(status: str, conclusion: Optional[str]) -> str:
            if status != "completed":
                return "pending"
            conc = (conclusion or "").lower()
            if conc in _DONE_CONCLUSIONS:
                return "success"
            if conc in _RETRY_CONCLUSIONS:
                return "failed"
            return "not_retryable"

        def _sync_trackers_from_server() -> None:
            """Pull tracker state from the canonical server. Snapshot only — no network.

            Crucially, the prologue may have already fetched PR titles, run IDs,
            and backport metadata via the GitHub API. The first few sync ticks
            run before the server has polled the tracker (last_updated == 0),
            and `tracker[...]` carries empty/zero defaults. We must NOT clobber
            the prologue's data in that window — otherwise resumed sessions
            render as "PR title unavailable" until the next server tick lands.
            """
            try:
                snapshot = _rpc_server.snapshot_trackers()
            except Exception:
                return
            server_urls = {str(t.get("target", "")).strip(): t for t in snapshot if t.get("target")}

            for url, tracker in server_urls.items():
                jobs = list(tracker.get("jobs") or [])
                tracker_polled = float(tracker.get("last_updated", 0) or 0) > 0
                if url not in target_state:
                    target_state[url] = {
                        "source": url,
                        "status_hint": "",
                        "run_ids": [],
                        "pr_title": "",
                        "pr_base_title": "",
                        "is_backport": False,
                        "backport_target": "",
                        "backport_source_pr": 0,
                        "tracker_id": 0,
                    }
                    _event(f"tracking {url}")
                # Always link to the tracker id.
                target_state[url]["tracker_id"] = int(tracker.get("id", 0))

                if tracker_polled:
                    # Refresh metadata, but only overwrite when the server actually has data.
                    detail = str(tracker.get("last_detail", "") or "")
                    if detail:
                        target_state[url]["status_hint"] = detail
                    run_ids_from_jobs = [int(j.get("id", 0) or 0) for j in jobs if int(j.get("id", 0) or 0) > 0]
                    if run_ids_from_jobs:
                        target_state[url]["run_ids"] = run_ids_from_jobs
                    for field in ("pr_title", "pr_base_title", "backport_target"):
                        val = str(tracker.get(field, "") or "")
                        if val:
                            target_state[url][field] = val
                    if tracker.get("is_backport"):
                        target_state[url]["is_backport"] = True
                    src = int(tracker.get("backport_source_pr", 0) or 0)
                    if src:
                        target_state[url]["backport_source_pr"] = src

                for job in jobs:
                    rid = int(job.get("id", 0) or 0)
                    if rid <= 0:
                        continue
                    run_to_target[rid] = url
                    status = str(job.get("status", "") or "")
                    conclusion = str(job.get("conclusion", "") or "") or None
                    prev = state.get(rid, {})
                    state[rid] = {
                        # Prefer the prologue's real WorkflowRun (richer attrs) when present.
                        "run": prev.get("run") or _RunShim(job),
                        "repo_name": str(tracker.get("repo", "") or prev.get("repo_name", "")),
                        "retries": int(tracker.get("attempts_used", 0) or 0),
                        "done": status == "completed",
                        "result": _derive_result(status, conclusion),
                        "last_status": status,
                        "last_conclusion": conclusion,
                        "failed_jobs": list(job.get("failed_jobs") or prev.get("failed_jobs") or []),
                        "failed_jobs_conclusion": conclusion if (conclusion and conclusion.lower() in _RETRY_CONCLUSIONS) else None,
                        # Lazily-loaded grepped log excerpt (None until fetched).
                        # Cleared when the run's conclusion changes so stale logs don't linger.
                        "log_excerpt": (
                            prev.get("log_excerpt")
                            if prev.get("last_conclusion") == conclusion
                            else None
                        ),
                    }

            # Drop trackers + their runs that no longer exist server-side.
            for url in list(target_state.keys()):
                if url in server_urls:
                    continue
                for rid in list(target_state[url].get("run_ids", [])):
                    state.pop(rid, None)
                    run_to_target.pop(rid, None)
                target_state.pop(url, None)
                _event(f"untracking {url}")

            # Drop state entries whose target no longer exists. Don't GC by
            # tracker.jobs — that wipes prologue-populated runs before the
            # server's first poll.
            for rid in list(state.keys()):
                url = run_to_target.get(rid)
                if url is None or url not in target_state:
                    state.pop(rid, None)
                    run_to_target.pop(rid, None)

        while True:
            if shutdown_requested:
                break

            _sync_trackers_from_server()

            pending = [s for s in state.values() if not s["done"]]
            if not pending and not (interactive_tty or serve):
                _event("All runs finished.")
                break

            # Emit one-shot completion events for runs that just transitioned to done.
            # All status/retry/rerun logic lives in the server tracker — the TUI
            # only observes and reacts; no blocking GitHub calls here.
            for rid, s in list(state.items()):
                if not s["done"] or rid in _announced_done:
                    continue
                _announced_done.add(rid)
                label = f"[{s['repo_name']}#{rid}]"
                if s["result"] == "success":
                    _event(f"{label} {(s['last_conclusion'] or 'success').upper()}")
                elif s["result"] == "not_retryable":
                    _event(f"{label} concluded: {s['last_conclusion'] or '-'} — not retrying.")
                elif s["result"] == "failed":
                    conc = (s["last_conclusion"] or "failed")
                    _event(f"{label} {conc} — server retries exhausted.")

            if use_rich_tui and live_obj is not None:
                live_obj.update(_build_rich_dashboard())
            elif use_rolling:
                _render_dashboard()

            if shutdown_requested:
                break

            # Sleep in short ticks so key presses are handled responsively.
            _KEY_TICK = 0.1
            elapsed = 0.0
            while elapsed < interval:
                if shutdown_requested:
                    break
                time.sleep(_KEY_TICK)
                elapsed += _KEY_TICK

                if window_resized:
                    window_resized = False
                    if use_rich_tui and live_obj is not None:
                        live_obj.update(_build_rich_dashboard())
                    elif use_rolling:
                        _render_dashboard()

                if use_rich_tui and _handle_ui_keys():
                    if live_obj is not None:
                        live_obj.update(_build_rich_dashboard())

                # An `E` keystroke defers the editor launch to here, where the
                # Live display and raw terminal mode can be safely suspended.
                edit_repo = ui_state.pop("pending_edit_repo", None)
                if use_rich_tui and edit_repo:
                    current = _get_repo_log_filter(edit_repo)
                    seed = current if current.strip() else _default_log_filter_template(edit_repo)
                    try:
                        if live_obj is not None:
                            live_obj.stop()
                        if original_tty is not None and termios is not None and sys.stdin.isatty():
                            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_tty)
                        edited = click.edit(text=seed, extension=".conf")
                    finally:
                        if original_tty is not None and tty is not None and sys.stdin.isatty():
                            tty.setcbreak(sys.stdin.fileno())
                        if live_obj is not None:
                            live_obj.start()
                    if edited is not None:
                        try:
                            _parse_log_filter(edited)  # validate before saving
                            _set_repo_log_filter(edit_repo, edited)
                            # Drop cached excerpts so they re-grep with the new config.
                            _rpc_server._log_excerpt_cache.clear()
                            for s in state.values():
                                s["log_excerpt"] = None
                            _event(f"edited log filter for {edit_repo}")
                        except click.BadParameter as exc:
                            _event(f"log filter not saved: {exc.message}")
                    else:
                        _event("log filter unchanged")
                    if live_obj is not None:
                        live_obj.update(_build_rich_dashboard())
    except KeyboardInterrupt:
        shutdown_requested = True
        _event("Interrupted by user.")
        if use_rich_tui and live_obj is not None:
            live_obj.update(_build_rich_dashboard())
        elif use_rolling:
            _render_dashboard()
    finally:
        # Render final summary before stopping TUI
        if use_rich_tui and live_obj is not None:
            live_obj.update(_build_rich_dashboard())
        elif use_rolling:
            _render_dashboard()
        # Restore original signal handlers and TTY
        if hasattr(signal, "SIGWINCH"):
            signal.signal(signal.SIGWINCH, original_sigwinch)
        if original_sigint is not None:
            signal.signal(signal.SIGINT, original_sigint)
        if original_tty is not None and termios is not None and sys.stdin.isatty():
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, original_tty)
        if live_obj is not None:
            live_obj.stop()

        # Cleanly shut down the background asyncio loop so PyGithub calls
        # in flight on the executor don't see "cannot schedule new futures
        # after shutdown" during interpreter teardown.
        try:
            if _server_loop.is_running():
                async def _cancel_all() -> None:
                    for t in asyncio.all_tasks(loop=_server_loop):
                        t.cancel()
                asyncio.run_coroutine_threadsafe(_cancel_all(), _server_loop).result(timeout=2)
                _server_loop.call_soon_threadsafe(_server_loop.stop)
        except Exception:
            pass
        try:
            _server_thread.join(timeout=3)
        except Exception:
            pass

    if not use_rich_tui and not quiet:
        success_count = sum(1 for s in state.values() if s["result"] == "success")
        failed_runs = [s for s in state.values() if s["result"] in {"failed", "api_error", "not_retryable"}]
        click.echo()
        click.echo(
            f"Final summary: {success_count}/{len(state)} run(s) succeeded, "
            f"{len(failed_runs)} need attention."
        )
        for s in failed_runs:
            run = s["run"]
            target = run_to_target.get(run.id, "")
            click.echo(f"  - {_short_target(target)} -> {getattr(run, 'html_url', '')} ({s['result']})")


# ---------------------------------------------------------------------------
# failed-logs subcommand
# ---------------------------------------------------------------------------

@main.command("logs")
@click.argument("targets", nargs=-1, metavar="[TARGETS]...")
@click.option(
    "-t", "--token",
    envvar="GITHUB_TOKEN",
    default=None,
    callback=_token_option_callback,
    help="GitHub PAT (env: GITHUB_TOKEN; falls back to saved token from `auth`).",
)
@click.option(
    "-R", "--repo", "repo_opt",
    default=None,
    metavar="OWNER/REPO",
    help="Repo (owner/repo) — required for bare workflow run IDs.",
)
@click.option(
    "--grep", "grep_pattern",
    default=None,
    metavar="REGEX",
    help="Override the saved log_filter: print only matching lines (supports trailing +N/-N context).",
)
@click.option(
    "--context",
    default=2,
    show_default=True,
    type=click.IntRange(0, 50),
    help="Symmetric lines around each --grep match (when --grep has no +N/-N tokens).",
)
@click.option(
    "--all-jobs",
    is_flag=True,
    default=False,
    help="Show every failed job, bypassing the log_filter's job-name selection.",
)
def failed_logs_cmd(
    targets: tuple[str, ...],
    token: str,
    repo_opt: Optional[str],
    grep_pattern: Optional[str],
    context: int,
    all_jobs: bool,
) -> None:
    """Print failed workflow jobs and their logs.

    Uses the per-repo ``log_filter`` config (``ghelper config edit-log-filter``) to
    pick which failed job to show and how to grep it.  ``--grep`` overrides it.
    """
    g = Github(token)

    # Build the CLI grep override, if any (supports "REGEX +N -N").
    cli_spec: Optional[_GrepSpec] = None
    if grep_pattern is not None:
        cli_spec = _parse_grep_spec(grep_pattern)
        if cli_spec.before == 0 and cli_spec.after == 0:
            cli_spec = _GrepSpec(cli_spec.pattern, before=context, after=context)

    # Per-repo log_filter, parsed lazily and cached by repo.
    _filter_cache: dict[str, _LogFilter] = {}

    def _filter_for(repo_name: Optional[str]) -> _LogFilter:
        if cli_spec is not None:
            return _LogFilter(cli_spec, jobs=[])
        key = repo_name or ""
        if key not in _filter_cache:
            parsed_filter = _parse_log_filter(_get_repo_log_filter(key)) if key else _LogFilter(_GrepSpec(None), [])
            if all_jobs:
                parsed_filter = _LogFilter(parsed_filter.global_grep, jobs=[])
            _filter_cache[key] = parsed_filter
        return _filter_cache[key]

    raw_text_parts: list[str] = []

    if not sys.stdin.isatty():
        raw_text_parts.append(sys.stdin.read())

    if targets:
        raw_text_parts.append("\n".join(targets))

    if not raw_text_parts and sys.stdout.isatty():
        click.echo(
            "Paste PR/run URLs or a backport-tracker summary, one per line.\n"
            "Empty line or Ctrl-D to start:"
        )
        lines: list[str] = []
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                click.echo()
                break
            if not line:
                break
            lines.append(line)
        raw_text_parts.append("\n".join(lines))

    combined = "\n".join(raw_text_parts)
    parsed = _parse_summary(combined)

    if not parsed.entries:
        raise click.UsageError(
            "No targets found. Pass URLs / run IDs, or pipe backport-tracker output."
        )

    any_failed_jobs = False

    click.echo(f"Resolving targets...", err=True)
    for entry in parsed.entries:
        try:
            resolved = _resolve_target(entry.url, repo_opt, g)
        except GithubException as exc:
            raise click.ClickException(f"Cannot resolve {entry.url!r}: {_exc_message(exc)}")

        log_filter = _filter_for(_target_repo(entry.url, repo_opt))

        for run in resolved:
            click.echo(f"Fetching failed jobs from {_short_target(run.html_url)}...", err=True)
            try:
                failed_jobs = _collect_failed_jobs(run)
            except GithubException as exc:
                raise click.ClickException(
                    f"Cannot load jobs for {run.html_url}: {_exc_message(exc)}"
                )
            if not failed_jobs:
                click.echo(f"{_short_target(run.html_url)}: no failed jobs found")
                continue

            selected = _select_failed_jobs(failed_jobs, log_filter)
            if not selected:
                continue

            any_failed_jobs = True
            run_header_shown = False
            shown_in_run = 0

            for job_index, (job, spec) in enumerate(selected, 1):
                click.echo(f"  Downloading logs ({job_index}/{len(selected)}) for {job.name}...", err=True)
                blob = _download_binary(_job_logs_url(job), token)
                click.echo(f"    Parsing logs...", err=True)
                file_renders = [
                    (file_name, _extract_log_lines(log_text, spec))
                    for file_name, log_text in _decode_log_archive(blob)
                ]
                has_match = any(rendered for _, rendered in file_renders)

                # Fall through to the next job when a grep pattern is set but the
                # job's log produced no match.
                if spec.pattern is not None and not has_match:
                    click.echo(f"    {job.name}: no lines matched — trying next job", err=True)
                    continue

                if not run_header_shown:
                    click.echo(f"{_short_target(run.html_url)}")
                    run_header_shown = True
                shown_in_run += 1

                click.echo(f"  job: {job.name} ({job.conclusion})")
                failed_steps = [
                    step for step in (job.steps or [])
                    if (step.conclusion or "").lower() in _RETRY_CONCLUSIONS
                ]
                if failed_steps:
                    click.echo("    failed steps:")
                    for step in failed_steps:
                        click.echo(f"      - {step.name} ({step.conclusion})")
                else:
                    click.echo("    failed steps: (none reported by API)")

                for file_name, rendered in file_renders:
                    click.echo(f"    log: {file_name}")
                    if not rendered:
                        click.echo("      (no matching lines)")
                        continue
                    for line in rendered:
                        click.echo(f"      {line}")

            if shown_in_run == 0:
                click.echo(
                    f"{_short_target(run.html_url)}: no lines matched the filter "
                    f"in {len(selected)} selected job(s)"
                )

    if not any_failed_jobs:
        click.echo("No failed jobs found in the requested targets.")


# Headless HTTP server is exposed via `ghelper watch --serve --no-tui`.
