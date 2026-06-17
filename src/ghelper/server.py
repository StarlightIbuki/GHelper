"""
JSON-RPC 2.0 server for ghelper.
Allows JS scripts to push status updates and query cached data.
"""

import asyncio
import copy
import inspect
import json
import logging
import os
import threading
import webbrowser
from collections import deque
from pathlib import Path
import re
from typing import Any, Optional
import time
import urllib.error
import urllib.parse
import urllib.request

from aiohttp import web
import click
from github import Github

from ghelper.cli import (
    _collect_assigned_pr_entries,
    _collect_failed_jobs,
    _collect_structured_entries,
    _build_pr_display_meta,
    _count_approved_reviews,
    _decode_log_archive,
    _download_binary,
    _extract_log_lines,
    _get_repo_log_filter,
    _job_logs_url,
    _log_filter_preview as _cli_log_filter_preview,
    _log_filter_to_struct,
    _parse_log_filter,
    _pick_pr_status,
    _resolve_session_ref,
    _select_failed_jobs,
    _set_repo_log_filter,
    _struct_to_log_filter,
    _request_device_code as _cli_request_device_code,
    _poll_device_token as _cli_poll_device_token,
    _trigger_rerun,
    _trigger_update_branch,
    _URL_RE,
    _RETRY_CONCLUSIONS,
    _DONE_CONCLUSIONS,
)

logger = logging.getLogger(__name__)


def _detail_from_cached_jobs(jobs: list[dict], ignored: set[str]) -> str:
    """Derive a CI status string from a cached list of workflow-run summaries.

    Mirrors _pick_pr_status logic but operates on already-stored data so it can
    be applied cheaply whenever ignore_jobs or the job list changes (startup,
    ignore-config edits, token-missing refreshes).

    Returns an empty string when the jobs list is empty so callers can decide
    whether to fall back to "CI unavailable" or preserve the previous detail.
    """
    if not jobs:
        return ""
    has_pending = False
    has_failure = False
    has_success = False
    for job in jobs:
        status = str(job.get("status", "") or "")
        conclusion = str(job.get("conclusion", "") or "").lower()
        name = str(job.get("name", "") or "").strip().lower()
        if status != "completed":
            has_pending = True
            continue
        if conclusion in _RETRY_CONCLUSIONS:
            if name in ignored:
                continue
            # Also ignore when every individual failed job within this run is
            # covered by the ignore set (e.g. ignore_jobs lists a job name
            # inside a workflow rather than the workflow run name itself).
            failed = [str(f).strip().lower() for f in (job.get("failed_jobs") or []) if str(f).strip()]
            if failed and all(f in ignored for f in failed):
                continue
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
    return ""


_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<num>\d+)"
)


def _normalize_target_url(target: str) -> str:
    """Lowercase the owner/repo segment of a GitHub PR URL.

    GitHub treats owner/repo as case-insensitive, so two trackers added for
    e.g. `Kong/kong-ee#123` and `kong/kong-ee#123` should canonicalize to the
    same target. This keeps storage and grouping consistent regardless of the
    user's input casing.
    """
    text = str(target or "").strip()
    if not text:
        return text
    match = _PR_URL_RE.search(text)
    if not match:
        return text
    canonical_repo = match.group("repo").lower()
    start, end = match.span("repo")
    return text[:start] + canonical_repo + text[end:]

_GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
_DEFAULT_GITHUB_CLIENT_ID = "Ov23lio3O4l5m3CE589o"
_GITHUB_CLIENT_ID_ENV_NAMES = ("GHELPER_GITHUB_CLIENT_ID", "GH_RERUNNER_GITHUB_CLIENT_ID", "GH_RERUNNER_OAUTH_CLIENT_ID")
_GITHUB_CLIENT_SECRET_ENV_NAMES = ("GHELPER_GITHUB_CLIENT_SECRET", "GH_RERUNNER_GITHUB_CLIENT_SECRET", "GH_RERUNNER_OAUTH_CLIENT_SECRET")


def _normalize_backport_target(value: Any) -> str:
    target = str(value or "").strip()
    if target in {"-", "--"}:
        return ""
    if target.lower() in {"unknown", "n/a", "na", "none"}:
        return ""
    return target


def _get_device_flow_client_id() -> str:
    """Get the OAuth client_id for device flow (no secret required)."""
    client_id = ""
    for env_name in _GITHUB_CLIENT_ID_ENV_NAMES:
        client_id = os.environ.get(env_name, "").strip()
        if client_id:
            break
    if not client_id:
        client_id = _DEFAULT_GITHUB_CLIENT_ID
    return client_id


def _oauth_client_config() -> tuple[str, str]:
    client_id = _get_device_flow_client_id()
    client_secret = ""
    for env_name in _GITHUB_CLIENT_SECRET_ENV_NAMES:
        client_secret = os.environ.get(env_name, "").strip()
        if client_secret:
            break
    if not client_secret:
        raise click.ClickException(
            "OAuth auth requires GHELPER_GITHUB_CLIENT_SECRET (or the legacy GH_RERUNNER_* names)."
        )
    return client_id, client_secret


def _exchange_oauth_code(code: str) -> str:
    client_id, client_secret = _oauth_client_config()
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _GITHUB_OAUTH_TOKEN_URL,
        data=payload,
        headers={"Accept": "application/json", "User-Agent": "ghelper"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise click.ClickException("Unexpected OAuth response from GitHub.")
    error = data.get("error")
    if error:
        description = data.get("error_description") or str(error)
        raise click.ClickException(f"GitHub OAuth exchange failed: {description}")
    token = data.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise click.ClickException("GitHub did not return an access token.")
    return token.strip()


def _apply_token(server: "JSONRPCServer", token: str) -> str:
    token = token.strip()
    if not token:
        raise click.ClickException("Token is required.")
    server.token = token
    server._gh = Github(token)
    os.environ["GITHUB_TOKEN"] = token
    server.user_login = server._gh.get_user().login
    # Persist so the token survives a restart (same as _apply_gh_token in cli.py).
    try:
        from ghelper.cli import _load_user_config, _save_user_config
        cfg = _load_user_config()
        cfg["token"] = token
        _save_user_config(cfg)
    except Exception:
        logger.warning("Failed to persist token to config", exc_info=True)
    # After login, force a full tracker sync so stale statuses are refreshed quickly.
    refreshed = server.force_refresh_sync()
    if refreshed > 0:
        server._record_event(f"authenticated as {server.user_login}; full sync queued for {refreshed} tracker(s)")
    return server.user_login


def _clear_token(server: "JSONRPCServer") -> None:
    server.token = None
    server._gh = None
    server.user_login = None
    os.environ.pop("GITHUB_TOKEN", None)
    # Remove the persisted token from ~/.ghelper.json so it is not
    # reloaded on the next startup.
    try:
        from ghelper.cli import _load_user_config, _save_user_config
        cfg = _load_user_config()
        if "token" in cfg:
            del cfg["token"]
            _save_user_config(cfg)
    except Exception:
        logger.warning("Failed to remove token from persisted config", exc_info=True)


_WEB_DIR = Path(__file__).parent / "web"


class JSONRPCError(Exception):
    """Base JSON-RPC error"""
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data


class JSONRPCServer:
    """JSON-RPC 2.0 server for ghelper status syncing and control"""

    # JSON-RPC error codes (standard)
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603
    
    # Custom error codes
    SERVER_ERROR = -32000
    INVALID_TOKEN = -32001
    CACHE_ERROR = -32002

    def __init__(
        self,
        token: Optional[str] = None,
        cache_path: Path = Path.home() / ".ghelper-cache.json",
        session_path: Path = Path.home() / ".ghelper-sessions.json",
        trackers_path: Path = Path.home() / ".ghelper-trackers.json",
        repo_configs_path: Path = Path.home() / ".ghelper-repo-configs.json",
    ):
        """
        Initialize JSON-RPC server.
        
        Args:
            token: Optional GitHub token for server-side API access
            cache_path: Path to cache file
            session_path: Path to session file
        """
        self.token = token
        self.user_login: Optional[str] = None
        self.cache_path = cache_path
        self.session_path = session_path
        self.trackers_path = trackers_path
        self.repo_configs_path = repo_configs_path
        self._cache = self._load_cache()
        self._gh: Optional[Github] = Github(token) if token else None
        if token:
            try:
                self.user_login = self._gh.get_user().login if self._gh else None
            except Exception:
                self.user_login = None
        # Re-entrant lock so sync (CLI thread) and async (event-loop thread) paths
        # can both safely mutate _trackers.
        self._tracker_lock = threading.RLock()
        self._trackers = self._load_trackers()
        self._repo_configs: dict[str, dict] = self._load_repo_configs()
        self._rehydrate_trackers_from_cache()
        self._tracker_task: Optional[asyncio.Task[Any]] = None
        # Activity log shared by polling loop, sync wrappers, and TUI.
        self.events: deque[str] = deque(maxlen=5000)
        # Lazily-fetched, grepped failed-job log excerpts, keyed by
        # (run_id, log_filter text) so re-greps after a config edit re-download.
        self._log_excerpt_cache: dict[tuple[int, str], dict[str, Any]] = {}
        # Bounds concurrent log downloads; created lazily inside the event loop.
        self._log_excerpt_sem: Optional[asyncio.Semaphore] = None

    # ------------------------------------------------------------------
    # Sync API for in-process callers (TUI thread)
    # ------------------------------------------------------------------

    def _record_event(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.events.append(f"[{stamp}] {msg}")

    def _effective_ignore_for(self, tracker: dict) -> set[str]:
        """Return the combined, lowercased ignore-jobs set for a tracker.

        Backport PRs (target branch != master/main) use the repo-level
        ``backport_ignore_jobs`` list; regular PRs use ``ignore_jobs``.
        The per-tracker ``ignore_jobs`` list is always appended for both.
        """
        repo = str(tracker.get("repo", "") or "")
        repo_cfg = self._repo_configs.get(repo, {})
        is_backport = bool(tracker.get("is_backport", False))
        repo_ignore_key = "backport_ignore_jobs" if is_backport else "ignore_jobs"
        repo_ignore = repo_cfg.get(repo_ignore_key, []) or []
        tracker_ignore = tracker.get("ignore_jobs", []) if isinstance(tracker.get("ignore_jobs"), list) else []
        return {
            str(x).strip().lower()
            for x in list(repo_ignore) + list(tracker_ignore)
            if str(x).strip()
        }

    def _recompute_detail(self, tracker: dict) -> bool:
        """Re-derive last_detail from cached jobs + effective ignore.  Returns True if the value changed."""
        detail = str(tracker.get("last_detail", "") or "")
        # Never clobber terminal states that came from the PR object itself.
        if detail in ("Merged", "Closed"):
            return False
        jobs = tracker.get("jobs") if isinstance(tracker.get("jobs"), list) else []
        recomputed = _detail_from_cached_jobs(jobs, self._effective_ignore_for(tracker))
        if recomputed and recomputed != detail:
            tracker["last_detail"] = recomputed
            return True
        return False

    def _get_cached_pr_entry(
        self,
        repo_name: str,
        pr_number: int,
        ttl_seconds: Optional[int] = 3600,
    ) -> Optional[dict[str, Any]]:
        prs = self._cache.get("prs", {})
        if not isinstance(prs, dict):
            return None
        # Owner/repo on GitHub is case-insensitive; match cache keys the same way.
        key_lower = f"{repo_name}#{pr_number}".lower()
        raw = next(
            (v for k, v in prs.items() if isinstance(k, str) and k.lower() == key_lower),
            None,
        )
        if not isinstance(raw, dict):
            return None

        ts = raw.get("ts")
        if not isinstance(ts, (int, float)):
            return None

        is_merged = bool(raw.get("is_merged", False))
        if ttl_seconds is not None and ttl_seconds > 0 and not is_merged and time.time() - float(ts) > ttl_seconds:
            return None

        return {
            "branch": str(raw.get("branch", "") or ""),
            "detail": str(raw.get("detail", "") or ""),
            "title": str(raw.get("title", "") or ""),
            "source_pr": int(raw.get("source_pr", 0) or 0),
            "backport_target": _normalize_backport_target(raw.get("backport_target", "")),
            "is_merged": is_merged,
        }

    def _prime_tracker_from_cache(self, tracker: dict[str, Any]) -> bool:
        target = str(tracker.get("target", "")).strip()
        m = _PR_URL_RE.search(target)
        if not m:
            return False

        repo_name = m.group("repo")
        pr_number = int(m.group("num"))
        # For tracker hydration we prefer stale metadata over empty placeholders.
        cached = self._get_cached_pr_entry(repo_name, pr_number, ttl_seconds=None)
        if not cached:
            return False

        title = str(cached.get("title", "") or "")
        branch = str(cached.get("branch", "") or "")
        detail = str(cached.get("detail", "") or "")

        if not title and not branch and not detail:
            return False

        meta = _build_pr_display_meta(title, branch, "")
        backport_target = _normalize_backport_target(meta.get("backport_target", ""))
        if not backport_target:
            backport_target = _normalize_backport_target(cached.get("backport_target", ""))

        # Preserve any canonical-cased repo previously written by _update_tracker;
        # the URL-extracted value here is always lowercase after URL normalization.
        if not str(tracker.get("repo", "") or ""):
            tracker["repo"] = repo_name
        tracker["pr_number"] = pr_number
        tracker["last_detail"] = detail or "unknown"
        tracker["last_error"] = ""
        tracker["last_action"] = "status-cached"
        tracker["pr_title"] = str(meta.get("pr_title", title) or title)
        tracker["pr_base_title"] = str(meta.get("pr_base_title", title) or title)
        tracker["is_backport"] = bool(meta.get("is_backport", False))
        tracker["backport_target"] = backport_target
        tracker["backport_source_pr"] = int(cached.get("source_pr", 0) or 0)
        tracker["next_check_ts"] = time.time() + max(5, int(tracker.get("interval_seconds", 60) or 60))
        return True

    def _rehydrate_trackers_from_cache(self) -> None:
        """Repopulate tracker metadata from cache on startup.

        This keeps existing tracker rows informative after restarts when no
        token is available yet.
        """
        changed = False
        for tracker in self._trackers:
            before = {
                "repo": str(tracker.get("repo", "") or ""),
                "pr_number": int(tracker.get("pr_number", 0) or 0),
                "pr_title": str(tracker.get("pr_title", "") or ""),
                "pr_base_title": str(tracker.get("pr_base_title", "") or ""),
                "is_backport": bool(tracker.get("is_backport", False)),
                "backport_target": str(tracker.get("backport_target", "") or ""),
                "backport_source_pr": int(tracker.get("backport_source_pr", 0) or 0),
                "last_detail": str(tracker.get("last_detail", "") or ""),
            }
            if self._prime_tracker_from_cache(tracker):
                after = {
                    "repo": str(tracker.get("repo", "") or ""),
                    "pr_number": int(tracker.get("pr_number", 0) or 0),
                    "pr_title": str(tracker.get("pr_title", "") or ""),
                    "pr_base_title": str(tracker.get("pr_base_title", "") or ""),
                    "is_backport": bool(tracker.get("is_backport", False)),
                    "backport_target": str(tracker.get("backport_target", "") or ""),
                    "backport_source_pr": int(tracker.get("backport_source_pr", 0) or 0),
                    "last_detail": str(tracker.get("last_detail", "") or ""),
                }
                if after != before:
                    changed = True
            # Always re-derive CI status from cached jobs + effective ignore so
            # changes to ignore_jobs are reflected on startup even before the
            # first live API call.
            if self._recompute_detail(tracker):
                changed = True
        if changed:
            self._save_trackers()

    def snapshot_trackers(self) -> list[dict[str, Any]]:
        """Deep copy of trackers for read-only consumers across threads."""
        with self._tracker_lock:
            return [copy.deepcopy(t) for t in self._trackers]

    def snapshot_events(self) -> list[str]:
        return list(self.events)

    def submit_tracker_sync(
        self,
        target: str,
        attempts: int = 2,
        interval_seconds: int = 60,
        auto_rerun: bool = True,
        ignore_jobs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        target = _normalize_target_url(str(target or "").strip())
        if not target:
            raise ValueError("target is required")
        if not _PR_URL_RE.search(target):
            raise ValueError("target must be a GitHub PR URL")
        ignore = [str(x).strip() for x in (ignore_jobs or []) if str(x).strip()]
        with self._tracker_lock:
            for existing in self._trackers:
                if _normalize_target_url(str(existing.get("target", "")).strip()) == target:
                    return copy.deepcopy(existing)
            next_id = max((int(t.get("id", 0) or 0) for t in self._trackers), default=0) + 1
            tracker = {
                "id": next_id,
                "target": target,
                "attempts_total": max(0, int(attempts)),
                "attempts_used": 0,
                "run_attempts": {},
                "retries_exhausted": False,
                "interval_seconds": max(5, int(interval_seconds)),
                "active": True,
                "auto_rerun": bool(auto_rerun),
                "last_detail": "",
                "last_error": "",
                "last_action": "created",
                "last_updated": 0.0,
                "next_check_ts": 0.0,
                "ignore_jobs": ignore,
                "repo": "",
                "pr_number": 0,
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
                "jobs": [],
                "approvals": 0,
                "labels": [],
            }
            self._prime_tracker_from_cache(tracker)
            self._trackers.append(tracker)
            self._save_trackers()
            self._record_event(f"added tracker {target}")
            return copy.deepcopy(tracker)

    def remove_tracker_sync(self, tracker_id: int) -> bool:
        with self._tracker_lock:
            before = len(self._trackers)
            self._trackers = [t for t in self._trackers if int(t.get("id", 0) or 0) != int(tracker_id)]
            removed = before != len(self._trackers)
            if removed:
                self._save_trackers()
                self._record_event(f"removed tracker {tracker_id}")
            return removed

    def force_refresh_sync(self, tracker_id: Optional[int] = None) -> int:
        with self._tracker_lock:
            count = 0
            for tracker in self._trackers:
                if tracker_id is not None and int(tracker.get("id", 0) or 0) != int(tracker_id):
                    continue
                tracker["next_check_ts"] = 0.0
                count += 1
            return count

    def set_active_sync(self, tracker_id: int, active: bool) -> bool:
        with self._tracker_lock:
            for tracker in self._trackers:
                if int(tracker.get("id", 0) or 0) == int(tracker_id):
                    tracker["active"] = bool(active)
                    tracker["last_action"] = "resumed" if active else "paused"
                    self._save_trackers()
                    return True
            return False

    def update_branch_tracker_sync(self, tracker_id: int) -> dict[str, Any]:
        """Update a tracked PR's branch with its base branch (live API call).

        Returns ``{"ok": bool, "error": str}``. This performs a synchronous
        GitHub API call so the CLI key handler can invoke it directly; the web
        path wraps it in a worker thread via :meth:`_tracker_update_branch`.
        """
        with self._tracker_lock:
            tracker = next(
                (t for t in self._trackers if int(t.get("id", 0) or 0) == int(tracker_id)),
                None,
            )
            if tracker is None:
                return {"ok": False, "error": "tracker not found"}
            target = str(tracker.get("target", "")).strip()

        if not self._gh:
            return {"ok": False, "error": "not authenticated"}
        match = _PR_URL_RE.search(target)
        if not match:
            return {"ok": False, "error": "target is not a PR URL"}
        repo_name = match.group("repo")
        pr_number = int(match.group("num"))

        try:
            repo = self._gh.get_repo(repo_name)
            pr = repo.get_pull(pr_number)
            mode = _trigger_update_branch(pr)
        except Exception as exc:
            msg = str(exc)
            with self._tracker_lock:
                tracker["last_error"] = msg
                tracker["last_action"] = "update-branch-failed"
            self._record_event(f"#{pr_number} {repo_name} update branch error: {msg}")
            return {"ok": False, "error": msg}

        with self._tracker_lock:
            tracker["last_error"] = ""
            tracker["last_action"] = "update-branch-triggered"
            # Re-check soon so the freshly-updated branch's CI is picked up.
            tracker["next_check_ts"] = 0.0
            self._save_trackers()
        self._record_event(f"#{pr_number} {repo_name} branch update queued [{mode}]")
        return {"ok": True, "error": ""}

    def log_excerpt_sync(
        self,
        tracker_id: int,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Synchronous failed-CI log excerpt for the in-process TUI.

        Mirrors :meth:`_tracker_log_excerpt` but performs the (blocking) download
        on the calling thread so the CLI key handler can invoke it directly.
        Results are cached by (run_id, log_filter text).
        """
        with self._tracker_lock:
            tracker = next(
                (t for t in self._trackers if int(t.get("id", 0) or 0) == int(tracker_id)),
                None,
            )
            if tracker is None:
                return {"run_id": run_id, "lines": [], "error": "tracker not found"}
            repo = str(tracker.get("repo", "") or "").strip()
            jobs = list(tracker.get("jobs") or [])

        if run_id is None:
            failing = next(
                (
                    j for j in jobs
                    if str(j.get("conclusion", "") or "").lower() in _RETRY_CONCLUSIONS
                ),
                None,
            )
            if failing is None:
                return {"run_id": None, "lines": [], "error": "no failing run on this tracker"}
            run_id = int(failing.get("id", 0) or 0)
        run_id = int(run_id)

        log_filter_text = _get_repo_log_filter(repo)
        cache_key = (run_id, log_filter_text)
        cached = self._log_excerpt_cache.get(cache_key)
        if cached is not None:
            return cached

        result = self._compute_log_excerpt(repo, run_id, log_filter_text)
        if not result.get("error"):
            self._log_excerpt_cache[cache_key] = result
        return result

    def _load_trackers(self) -> list[dict[str, Any]]:
        if not self.trackers_path.exists():
            return []
        try:
            data = json.loads(self.trackers_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            # Keep only dict entries and normalize persisted fields.
            normalized: list[dict[str, Any]] = []
            for raw in data:
                if not isinstance(raw, dict):
                    continue
                tracker_id = int(raw.get("id", 0) or 0)
                target = _normalize_target_url(str(raw.get("target", "")).strip())
                if tracker_id <= 0 or not target:
                    continue
                raw_jobs = raw.get("jobs", []) if isinstance(raw.get("jobs", []), list) else []
                jobs: list[dict[str, Any]] = []
                for item in raw_jobs:
                    if not isinstance(item, dict):
                        continue
                    rid = int(item.get("id", 0) or 0)
                    if rid <= 0:
                        continue
                    jobs.append({
                        "id": rid,
                        "name": str(item.get("name", "") or "workflow"),
                        "status": str(item.get("status", "") or ""),
                        "conclusion": str(item.get("conclusion", "") or ""),
                        "url": str(item.get("url", "") or ""),
                        "failed_jobs": [
                            str(x)
                            for x in (item.get("failed_jobs", []) if isinstance(item.get("failed_jobs", []), list) else [])
                            if str(x).strip()
                        ],
                    })
                normalized.append({
                    "id": tracker_id,
                    "target": target,
                    "attempts_total": int(_at if (_at := raw.get("attempts_total")) is not None else 2),
                    "attempts_used": int(raw.get("attempts_used", 0) or 0),
                    "run_attempts": (
                        {str(k): int(v or 0) for k, v in raw["run_attempts"].items()}
                        if isinstance(raw.get("run_attempts"), dict) else {}
                    ),
                    "retries_exhausted": bool(raw.get("retries_exhausted", False)),
                    "interval_seconds": int(raw.get("interval_seconds", 60) or 60),
                    "active": bool(raw.get("active", True)),
                    "auto_rerun": bool(raw.get("auto_rerun", True)),
                    "last_detail": str(raw.get("last_detail", "")),
                    "last_error": str(raw.get("last_error", "")),
                    "last_action": str(raw.get("last_action", "")),
                    "last_updated": float(raw.get("last_updated", 0) or 0),
                    "next_check_ts": float(raw.get("next_check_ts", 0) or 0),
                    "ignore_jobs": [
                        str(x).strip()
                        for x in (raw.get("ignore_jobs", []) if isinstance(raw.get("ignore_jobs", []), list) else [])
                        if str(x).strip()
                    ],
                    "repo": str(raw.get("repo", "") or ""),
                    "pr_number": int(raw.get("pr_number", 0) or 0),
                    "pr_title": str(raw.get("pr_title", "") or ""),
                    "pr_base_title": str(raw.get("pr_base_title", "") or ""),
                    "is_backport": bool(raw.get("is_backport", False)),
                    "backport_target": _normalize_backport_target(raw.get("backport_target", "")),
                    "backport_source_pr": int(raw.get("backport_source_pr", 0) or 0),
                    "jobs": jobs,
                    "approvals": int(raw.get("approvals", 0) or 0),
                    "labels": [
                        str(x)
                        for x in (raw.get("labels", []) if isinstance(raw.get("labels", []), list) else [])
                        if str(x).strip()
                    ],
                })
            # Drop duplicates introduced by case-different target URLs that
            # predate URL canonicalization. Keep the most recently updated.
            by_target: dict[str, dict[str, Any]] = {}
            for tracker in normalized:
                key = str(tracker.get("target", "")).strip()
                existing = by_target.get(key)
                if existing is None or float(tracker.get("last_updated", 0) or 0) > float(
                    existing.get("last_updated", 0) or 0
                ):
                    by_target[key] = tracker
            return list(by_target.values())
        except Exception as exc:
            logger.error("Failed to load trackers: %s", exc)
            return []

    def _save_trackers(self) -> None:
        try:
            data = json.dumps(self._trackers, indent=2, sort_keys=True) + "\n"
            tmp = self.trackers_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.trackers_path)
        except Exception as exc:
            logger.error("Failed to save trackers: %s", exc)

    def _load_repo_configs(self) -> dict[str, dict]:
        if not self.repo_configs_path.exists():
            return {}
        try:
            data = json.loads(self.repo_configs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            result: dict[str, dict] = {}
            for repo, cfg in data.items():
                if not isinstance(cfg, dict):
                    continue
                result[str(repo)] = {
                    "ignore_jobs": [
                        str(x).strip()
                        for x in (cfg.get("ignore_jobs", []) if isinstance(cfg.get("ignore_jobs", []), list) else [])
                        if str(x).strip()
                    ],
                    "required_reviews": max(0, int(cfg.get("required_reviews", 0) or 0)),
                    "required_labels": [
                        str(x).strip()
                        for x in (cfg.get("required_labels", []) if isinstance(cfg.get("required_labels", []), list) else [])
                        if str(x).strip()
                    ],
                    "backport_ignore_jobs": [
                        str(x).strip()
                        for x in (cfg.get("backport_ignore_jobs", []) if isinstance(cfg.get("backport_ignore_jobs", []), list) else [])
                        if str(x).strip()
                    ],
                    "backport_required_reviews": max(0, int(cfg.get("backport_required_reviews", 0) or 0)),
                    "backport_required_labels": [
                        str(x).strip()
                        for x in (cfg.get("backport_required_labels", []) if isinstance(cfg.get("backport_required_labels", []), list) else [])
                        if str(x).strip()
                    ],
                }
            return result
        except Exception as exc:
            logger.error("Failed to load repo configs: %s", exc)
            return {}

    def _save_repo_configs(self) -> None:
        try:
            data = json.dumps(self._repo_configs, indent=2, sort_keys=True) + "\n"
            tmp = self.repo_configs_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.repo_configs_path)
        except Exception as exc:
            logger.error("Failed to save repo configs: %s", exc)

    async def start_background_tasks(self) -> None:
        if self._tracker_task is None or self._tracker_task.done():
            self._tracker_task = asyncio.create_task(self._tracker_loop(), name="ghelper-tracker-loop")

    async def stop_background_tasks(self) -> None:
        if self._tracker_task is None:
            return
        self._tracker_task.cancel()
        try:
            await self._tracker_task
        except asyncio.CancelledError:
            pass
        self._tracker_task = None

    async def _tracker_loop(self) -> None:
        while True:
            try:
                await self._tracker_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Tracker loop tick failed; continuing")
            await asyncio.sleep(3)

    async def _tracker_tick(self) -> None:
        now = time.time()
        changed = False
        with self._tracker_lock:
            due: list[dict[str, Any]] = []
            for tracker in self._trackers:
                if not tracker.get("active", True):
                    continue
                next_check = float(tracker.get("next_check_ts", 0) or 0)
                if next_check > now:
                    continue
                due.append(tracker)

        for tracker in due:
            try:
                await asyncio.to_thread(self._update_tracker, tracker)
            except Exception:
                logger.exception("Tracker %s update raised", tracker.get("id"))
                tracker["last_error"] = "internal error"
                tracker["last_action"] = "update-failed"
                self._record_event(f"tracker {tracker.get('id')} update failed")
            tracker["next_check_ts"] = time.time() + max(
                5, int(tracker.get("interval_seconds", 60) or 60)
            )
            changed = True

        if changed:
            with self._tracker_lock:
                self._save_trackers()

    def _update_tracker(self, tracker: dict[str, Any]) -> bool:
        """Synchronous tracker refresh — caller must run in a worker thread."""
        target = str(tracker.get("target", "")).strip()
        tracker["last_updated"] = time.time()

        match = _PR_URL_RE.search(target)
        if match:
            repo_name = match.group("repo")
            pr_number = int(match.group("num"))
            # Don't clobber a canonical-cased repo (from a prior API call) with
            # the lowercase URL value — that produces a visible "bounce" between
            # refreshes when polling lands between the two writes below.
            if not str(tracker.get("repo", "") or ""):
                tracker["repo"] = repo_name
            tracker["pr_number"] = pr_number
        else:
            repo_name = ""
            pr_number = 0

        if not self._gh:
            # Keep previously known metadata/status visible while unauthenticated.
            self._prime_tracker_from_cache(tracker)
            tracker["last_error"] = "Server token missing"
            tracker["last_action"] = "idle"
            jobs = tracker.get("jobs") if isinstance(tracker.get("jobs"), list) else []
            tracker["jobs"] = jobs
            # Re-derive status from cached jobs + current ignore settings so
            # that changes to ignore_jobs take effect without a live API call.
            recomputed = _detail_from_cached_jobs(jobs, self._effective_ignore_for(tracker))
            if recomputed:
                tracker["last_detail"] = recomputed
            elif not tracker.get("last_detail"):
                tracker["last_detail"] = "unknown"
            return True

        if not match:
            tracker["last_error"] = "Target must be a PR URL"
            tracker["last_action"] = "invalid-target"
            return True
        try:
            repo = self._gh.get_repo(repo_name)
            # GitHub treats owner/repo case-insensitively; adopt the canonical
            # full_name so trackers added under different casings group together.
            canonical_repo = str(getattr(repo, "full_name", "") or repo_name) or repo_name
            if canonical_repo and canonical_repo != repo_name:
                repo_name = canonical_repo
            pr = repo.get_pull(pr_number)
            body = str(getattr(pr, "body", "") or "")
            title = str(getattr(pr, "title", "") or "")
            branch = str(getattr(getattr(pr, "head", None), "ref", "") or "")
            base_branch = str(getattr(getattr(pr, "base", None), "ref", "") or "")
            # A PR targeting any branch other than main/master is a backport.
            # Set this before _effective_ignore_for so the right ignore list is picked.
            is_backport = base_branch.lower() not in ("master", "main", "")
            tracker["is_backport"] = is_backport
            _effective_ignore = self._effective_ignore_for(tracker)
            detail = _pick_pr_status(repo, pr, _effective_ignore)
            meta = _build_pr_display_meta(title, branch, body)
            backport_target = _normalize_backport_target(meta.get("backport_target", ""))
            if is_backport and not backport_target:
                backport_target = base_branch
            tracker["last_error"] = ""
            tracker["last_action"] = "status-updated"
            tracker["repo"] = repo_name
            tracker["pr_number"] = pr_number
            tracker["pr_title"] = str(meta.get("pr_title", title) or title)
            tracker["pr_base_title"] = str(meta.get("pr_base_title", title) or title)
            tracker["backport_target"] = backport_target
            tracker["backport_source_pr"] = int(meta.get("backport_source_pr", 0) or 0)

            prs = self._cache.setdefault("prs", {})
            if not isinstance(prs, dict):
                self._cache["prs"] = {}
                prs = self._cache["prs"]
            prs["%s#%d" % (repo_name, pr_number)] = {
                "ts": time.time(),
                "branch": branch,
                "detail": detail,
                "title": title,
                "source_pr": int(meta.get("backport_source_pr", 0) or 0),
                "backport_target": backport_target,
                "is_merged": bool(getattr(pr, "merged", False)),
            }
            self._save_cache()

            try:
                runs = list(repo.get_workflow_runs(head_sha=pr.head.sha))
            except Exception:
                runs = []

            # A single workflow file can produce multiple runs for the same SHA
            # (e.g. when triggered by both `push` and `pull_request`). Keep only
            # the most recent run per workflow so the rerunner doesn't act on
            # stale duplicates and the UI doesn't render the same job twice.
            # `get_workflow_runs` returns runs newest-first, so the first
            # occurrence of each key wins.
            deduped_runs: list[Any] = []
            seen_run_keys: set[Any] = set()
            for run in runs:
                key = getattr(run, "workflow_id", None) or str(
                    getattr(run, "name", "") or ""
                ).strip().lower()
                if not key or key in seen_run_keys:
                    continue
                seen_run_keys.add(key)
                deduped_runs.append(run)
            runs = deduped_runs

            prev_jobs = {int(j.get("id", 0) or 0): j for j in (tracker.get("jobs") or [])}
            new_jobs: list[dict[str, Any]] = []
            for run in runs[:50]:
                rid = int(getattr(run, "id", 0) or 0)
                conclusion = str(getattr(run, "conclusion", "") or "")
                prev = prev_jobs.get(rid, {})
                failed_jobs: list[str] = list(prev.get("failed_jobs") or [])
                # Only re-fetch failed jobs when the conclusion changes or we lack a record.
                if conclusion.lower() in _RETRY_CONCLUSIONS and prev.get("conclusion") != conclusion:
                    try:
                        failed_jobs = [
                            str(getattr(job, "name", "") or "(unnamed job)")
                            for job in _collect_failed_jobs(run)
                        ]
                    except Exception:
                        failed_jobs = list(prev.get("failed_jobs") or [])
                new_jobs.append({
                    "id": rid,
                    "name": str(getattr(run, "name", "") or "workflow"),
                    "status": str(getattr(run, "status", "") or ""),
                    "conclusion": conclusion,
                    "url": str(getattr(run, "html_url", "") or ""),
                    "failed_jobs": failed_jobs,
                })
            tracker["jobs"] = new_jobs
            # Re-derive detail from new_jobs which now carry failed_jobs data,
            # allowing per-job name matching (not just workflow run name).
            # Preserve terminal states (Merged/Closed) from _pick_pr_status.
            if detail not in ("Merged", "Closed"):
                recomputed = _detail_from_cached_jobs(new_jobs, _effective_ignore)
                if recomputed:
                    detail = recomputed
            tracker["last_detail"] = detail

            # Fetch current labels and approval count for action-oriented reporting.
            try:
                tracker["labels"] = [
                    str(getattr(lbl, "name", "") or "")
                    for lbl in getattr(pr, "labels", [])
                    if str(getattr(lbl, "name", "") or "").strip()
                ]
            except Exception:
                tracker["labels"] = tracker.get("labels") or []
            repo_cfg = self._repo_configs.get(repo_name, {})
            req_reviews_key = "backport_required_reviews" if is_backport else "required_reviews"
            req_reviews = int(repo_cfg.get(req_reviews_key, 0) or 0)
            if req_reviews > 0:
                try:
                    tracker["approvals"] = _count_approved_reviews(pr)
                except Exception:
                    pass  # keep previous value
            else:
                tracker["approvals"] = 0

            # Per-workflow retry tracking: run_attempts maps workflow name -> rerun count
            raw_ra = tracker.get("run_attempts")
            run_attempts: dict[str, int] = (
                {str(k): int(v or 0) for k, v in raw_ra.items()}
                if isinstance(raw_ra, dict) else {}
            )
            attempts_total = int(tracker.get("attempts_total", 0) or 0)
            auto_rerun = bool(tracker.get("auto_rerun", True))
            # Reuse the effective ignore set already computed for _pick_pr_status.
            ignored = _effective_ignore
            # Map run-id → lowercased failed_jobs for the already-fetched window.
            # Used both for rerun selection and retries_exhausted computation.
            failed_jobs_by_id: dict[int, list[str]] = {
                int(j.get("id", 0) or 0): [
                    str(f).strip().lower() for f in (j.get("failed_jobs") or []) if str(f).strip()
                ]
                for j in new_jobs
            }

            def _should_rerun(run: Any) -> bool:
                run_name = str(getattr(run, "name", "") or "").strip().lower()
                if run_name in ignored:
                    return False
                # Also skip when every failed job within the run is ignored.
                rid = int(getattr(run, "id", 0) or 0)
                fj = failed_jobs_by_id.get(rid)
                if fj is not None and fj and all(f in ignored for f in fj):
                    return False
                return True

            if runs and auto_rerun and detail == "CI failed" and attempts_total > 0:
                failed_runs_to_retry = [
                    run
                    for run in runs
                    if run.status == "completed"
                    and (run.conclusion or "").lower() in _RETRY_CONCLUSIONS
                    and _should_rerun(run)
                    and run_attempts.get(
                        str(getattr(run, "name", "") or "").strip().lower(), 0
                    ) < attempts_total
                ]
                rerun_job_entries: list[dict[str, Any]] = []
                for failed_run in failed_runs_to_retry:
                    run_name = str(getattr(failed_run, "name", "") or "").strip().lower()
                    try:
                        rerun_mode = _trigger_rerun(failed_run)
                    except Exception as exc:
                        self._record_event(
                            f"#{pr_number} {repo_name} rerun error for "
                            f"{getattr(failed_run, 'name', '')}: {exc}"
                        )
                        continue
                    run_attempts[run_name] = run_attempts.get(run_name, 0) + 1
                    self._record_event(
                        f"#{pr_number} {repo_name} rerunning {getattr(failed_run, 'name', '')} "
                        f"({run_attempts[run_name]}/{attempts_total}) [{rerun_mode}]"
                    )
                    rerun_job_entries.append({
                        "id": int(getattr(failed_run, "id", 0) or 0),
                        "name": str(getattr(failed_run, "name", "") or "workflow"),
                        "status": "queued",
                        "conclusion": "",
                        "url": str(getattr(failed_run, "html_url", "") or ""),
                        "failed_jobs": [],
                    })
                if rerun_job_entries:
                    tracker["run_attempts"] = run_attempts
                    tracker["attempts_used"] = max(run_attempts.values(), default=0)
                    tracker["last_action"] = "rerun-triggered:%s" % ",".join(
                        str(e["id"]) for e in rerun_job_entries
                    )
                    jobs = tracker.get("jobs") if isinstance(tracker.get("jobs"), list) else []
                    for entry in reversed(rerun_job_entries):
                        jobs.insert(0, entry)
                    tracker["jobs"] = jobs[:8]
            # Compute retries_exhausted: True when every currently-failing eligible run
            # has reached the per-workflow retry cap.
            if attempts_total > 0 and detail == "CI failed" and runs:
                failing_names = [
                    str(getattr(run, "name", "") or "").strip().lower()
                    for run in runs
                    if run.status == "completed"
                    and (run.conclusion or "").lower() in _RETRY_CONCLUSIONS
                    and _should_rerun(run)
                ]
                tracker["retries_exhausted"] = bool(failing_names) and all(
                    run_attempts.get(name, 0) >= attempts_total for name in failing_names
                )
            else:
                tracker["retries_exhausted"] = False
            return True
        except Exception as exc:
            tracker["last_error"] = str(exc)
            tracker["last_action"] = "update-failed"
            tracker["last_detail"] = "unknown"
            tracker["pr_title"] = "unknown"
            tracker["pr_base_title"] = "unknown"
            tracker["jobs"] = []
            self._record_event(f"tracker {tracker.get('id')} error: {exc}")
            return True

    def _load_cache(self) -> dict[str, Any]:
        """Load cache from disk"""
        if not self.cache_path.exists():
            return {"prs": {}}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"prs": {}}
            prs = data.get("prs")
            if not isinstance(prs, dict):
                data["prs"] = {}
            return data
        except Exception as e:
            logger.error("Failed to load cache: %s", e)
            return {"prs": {}}

    def _save_cache(self) -> None:
        """Save cache to disk"""
        try:
            prs = self._cache.get("prs", {})
            if not isinstance(prs, dict):
                self._cache["prs"] = {}
            data = json.dumps(self._cache, indent=2, sort_keys=True) + "\n"
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            os.replace(tmp, self.cache_path)
        except Exception as e:
            logger.error("Failed to save cache: %s", e)

    def _load_sessions(self) -> list[dict[str, Any]]:
        """Load sessions from disk"""
        if not self.session_path.exists():
            return []
        try:
            data = json.loads(self.session_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return []
        except Exception:
            return []

    async def handle_rpc(self, request: web.Request) -> web.Response:
        """Handle JSON-RPC 2.0 request"""
        try:
            if request.method != "POST":
                return self._error_response(None, self.INVALID_REQUEST, "POST required")

            # Parse JSON-RPC request
            try:
                body = await request.json()
            except json.JSONDecodeError:
                return self._error_response(None, self.PARSE_ERROR, "Invalid JSON")

            # Handle batch requests (array of requests)
            if isinstance(body, list):
                responses = []
                for req in body:
                    resp = await self._handle_single_request(req)
                    if resp is not None:  # Skip notifications
                        responses.append(resp)
                return web.json_response(responses if responses else [])

            # Handle single request
            resp = await self._handle_single_request(body)
            if resp is None:
                return web.Response(status=200)  # Notification, no response
            return web.json_response(resp)

        except Exception as e:
            logger.exception("Unhandled error in RPC handler")
            return self._error_response(None, self.INTERNAL_ERROR, str(e))

    async def _handle_single_request(self, req: Any) -> Optional[dict[str, Any]]:
        """Handle single JSON-RPC request"""
        if not isinstance(req, dict):
            return self._error_response(None, self.INVALID_REQUEST, "Request must be object")

        req_id = req.get("id")
        is_notification = "id" not in req

        try:
            if req.get("jsonrpc") != "2.0":
                return self._error_response(req_id, self.INVALID_REQUEST, "jsonrpc must be 2.0")

            method = req.get("method")
            if not isinstance(method, str):
                return self._error_response(req_id, self.INVALID_REQUEST, "method must be string")

            params = req.get("params", [])
            if isinstance(params, dict):
                args: tuple[Any, ...] = ()
                kwargs: dict[str, Any] = dict(params)
            elif isinstance(params, list):
                args = tuple(params)
                kwargs = {}
            else:
                return self._error_response(req_id, self.INVALID_PARAMS, "params must be list or object")

            result = await self._dispatch_method(method, *args, **kwargs)

            if is_notification:
                return None
            return self._success_response(req_id, result)

        except JSONRPCError as e:
            return self._error_response(req_id, e.code, e.message, e.data)
        except Exception as e:
            logger.exception("Error in method %s", req.get("method"))
            return self._error_response(req_id, self.INTERNAL_ERROR, str(e))

    async def _dispatch_method(self, method: str, *args, **kwargs) -> Any:
        """Dispatch RPC method call"""
        methods = {
            # Query methods
            "pushStatus": self._push_status,
            "getPRStatus": self._get_pr_status,
            "getAssignedPRs": self._get_assigned_prs,
            "getRunStatus": self._get_run_status,
            "cacheInfo": self._cache_info,
            "clearCache": self._clear_cache,
            "listSessions": self._list_sessions,
            "listMethods": self._list_methods,
            "resolveSession": self._resolve_session,
            # Tracker management
            "trackerList": self._tracker_list,
            "trackerAdd": self._tracker_add,
            "trackerAddTargets": self._tracker_add_targets,
            "trackerRemove": self._tracker_remove,
            "trackerAddAttempts": self._tracker_add_attempts,
            "trackerSetActive": self._tracker_set_active,
            "trackerUpdateBranch": self._tracker_update_branch,
            "trackerUpdate": self._tracker_update,
            "trackerRefresh": self._tracker_refresh,
            "trackerReorder": self._tracker_reorder,
            # Repo-level config
            "repoConfigGet": self._repo_config_get,
            "repoConfigSet": self._repo_config_set,
            "repoLogFilterGet": self._repo_log_filter_get,
            "repoLogFilterSet": self._repo_log_filter_set,
            "repoLogFilterGetStructured": self._repo_log_filter_get_structured,
            "repoLogFilterSetStructured": self._repo_log_filter_set_structured,
            "logFilterPreview": self._log_filter_preview,
            "logFilterParse": self._log_filter_parse,
            # Failed-CI log extraction
            "trackerLogExcerpt": self._tracker_log_excerpt,
            # Status methods
            "pushRunStatus": self._push_run_status,
            # Auth methods
            "requestDeviceCode": self._request_device_code,
            "pollDeviceToken": self._poll_device_token,
        }

        if method not in methods:
            raise JSONRPCError(self.METHOD_NOT_FOUND, "Method '%s' not found" % method)

        handler = methods[method]
        try:
            inspect.signature(handler).bind(*args, **kwargs)
        except TypeError as exc:
            raise JSONRPCError(self.INVALID_PARAMS, str(exc))

        if asyncio.iscoroutinefunction(handler):
            return await handler(*args, **kwargs)
        return handler(*args, **kwargs)

    async def _push_status(
        self,
        repo: str,
        pr_number: int,
        branch: str = "",
        detail: str = "",
        title: str = "",
        source_pr: int = 0,
        is_merged: bool = False,
    ) -> dict[str, str]:
        """Push PR status update from JS/client"""
        try:
            key = f"{repo}#{pr_number}"
            prs = self._cache.setdefault("prs", {})
            if not isinstance(prs, dict):
                self._cache["prs"] = {}
                prs = self._cache["prs"]

            prs[key] = {
                "ts": time.time(),
                "branch": branch,
                "detail": detail,
                "title": title,
                "source_pr": source_pr,
                "is_merged": is_merged,
            }
            self._save_cache()
            logger.info("Pushed status for %s", key)
            return {"ack": True, "key": key}
        except Exception as e:
            logger.error("Error pushing status: %s", e)
            raise JSONRPCError(self.CACHE_ERROR, "Failed to push status: %s" % e)

    async def _get_pr_status(self, repo: str, pr_number: int) -> Optional[dict[str, Any]]:
        """Get cached PR status"""
        try:
            key = f"{repo}#{pr_number}"
            prs = self._cache.get("prs", {})
            if not isinstance(prs, dict):
                return None
            
            raw = prs.get(key)
            if not isinstance(raw, dict):
                return None

            ts = raw.get("ts")
            if not isinstance(ts, (int, float)):
                return None

            # Check TTL unless merged (merged PRs persistent)
            is_merged = bool(raw.get("is_merged", False))
            ttl_seconds = 3600  # 1 hour
            if not is_merged and time.time() - float(ts) > ttl_seconds:
                return None

            return {
                "branch": raw.get("branch", ""),
                "detail": raw.get("detail", ""),
                "title": raw.get("title", ""),
                "source_pr": raw.get("source_pr", 0),
                "backport_target": raw.get("backport_target", ""),
                "is_merged": is_merged,
            }
        except Exception as e:
            logger.error("Error getting PR status: %s", e)
            raise JSONRPCError(self.CACHE_ERROR, "Failed to get PR status: %s" % e)

    async def _get_assigned_prs(
        self,
        repo: Optional[str] = None,
        include_closed: bool = False,
        include_drafts: bool = False,
        filter_pattern: Optional[str] = None,
    ) -> dict[str, Any]:
        """Fetch assigned PRs via server token for JS fallback mode."""
        if not self.token:
            raise JSONRPCError(
                self.INVALID_TOKEN,
                "Server has no GitHub token configured; provide --token or GITHUB_TOKEN",
            )

        try:
            gh = self._gh or Github(self.token)
            title, entries, metadata = await asyncio.to_thread(
                _collect_assigned_pr_entries,
                gh,
                repo_opt=repo,
                include_closed=include_closed,
                include_drafts=include_drafts,
                filter_pattern=filter_pattern,
                pr_status_cache=self._cache,
                save_cache=lambda _: self._save_cache(),
            )
            return {
                "title": title,
                "entries": [
                    {
                        "branch": branch,
                        "url": url,
                        "detail": detail,
                    }
                    for branch, url, detail in entries
                ],
                "metadata": metadata,
            }
        except JSONRPCError:
            raise
        except Exception as e:
            logger.error("Error fetching assigned PRs: %s", e)
            raise JSONRPCError(self.SERVER_ERROR, "Failed to fetch assigned PRs: %s" % e)

    async def _get_run_status(self, run_id: int) -> Optional[dict[str, Any]]:
        """Get run status cached via pushRunStatus."""
        runs = self._cache.get("runs", {})
        if not isinstance(runs, dict):
            return None
        raw = runs.get(str(run_id))
        if not isinstance(raw, dict):
            return None
        return {
            "run_id": run_id,
            "status": raw.get("status"),
            "conclusion": raw.get("conclusion"),
            "workflow_name": raw.get("workflow_name"),
            "updated_at": raw.get("ts"),
        }

    async def _cache_info(self) -> dict[str, Any]:
        """Get cache information"""
        try:
            prs = self._cache.get("prs", {})
            runs = self._cache.get("runs", {})
            return {
                "size": len(prs) if isinstance(prs, dict) else 0,
                "entries": list(prs.keys())[:10] if isinstance(prs, dict) else [],
                "runs": len(runs) if isinstance(runs, dict) else 0,
                "trackers": len(self._trackers),
                "ttl_seconds": 3600,
            }
        except Exception as e:
            raise JSONRPCError(self.CACHE_ERROR, f"Failed to get cache info: {e}")

    async def _list_methods(self) -> dict[str, list[str]]:
        """List available RPC methods for debugging and client capability checks."""
        return {
            "methods": [
                "pushStatus",
                "getPRStatus",
                "getAssignedPRs",
                "getRunStatus",
                "pushRunStatus",
                "cacheInfo",
                "clearCache",
                "listSessions",
                "resolveSession",
                "listMethods",
                "trackerList",
                "trackerAdd",
                "trackerAddTargets",
                "trackerRemove",
                "trackerAddAttempts",
                "trackerSetActive",
                "trackerUpdateBranch",
                "trackerRefresh",
                "trackerReorder",
            ]
        }

    async def _tracker_list(self) -> dict[str, Any]:
        with self._tracker_lock:
            items = [dict(item) for item in self._trackers]
        return {"trackers": items, "repo_configs": dict(self._repo_configs)}

    async def _tracker_reorder(self, ordered_ids: list[int]) -> dict[str, Any]:
        ids = [int(x) for x in ordered_ids if isinstance(x, (int, float)) or (isinstance(x, str) and x.lstrip('-').isdigit())]
        with self._tracker_lock:
            id_to_tracker = {int(t.get("id", 0) or 0): t for t in self._trackers}
            reordered: list[dict[str, Any]] = []
            seen: set[int] = set()
            for tid in ids:
                if tid in id_to_tracker and tid not in seen:
                    reordered.append(id_to_tracker[tid])
                    seen.add(tid)
            # Append any trackers not mentioned in ordered_ids (e.g. newly added)
            for t in self._trackers:
                tid = int(t.get("id", 0) or 0)
                if tid not in seen:
                    reordered.append(t)
            self._trackers = reordered
            self._save_trackers()
        return {"ok": True}

    async def _tracker_add(
        self,
        target: str,
        attempts: int = 2,
        interval_seconds: int = 60,
        auto_rerun: bool = True,
        ignore_jobs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        target = _normalize_target_url(str(target or "").strip())
        if not target:
            raise JSONRPCError(self.INVALID_PARAMS, "target is required")
        if not _PR_URL_RE.search(target):
            raise JSONRPCError(self.INVALID_PARAMS, "target must be a GitHub PR URL")

        ignore = [
            str(x).strip()
            for x in (ignore_jobs or [])
            if str(x).strip()
        ]

        with self._tracker_lock:
            for existing in self._trackers:
                if _normalize_target_url(str(existing.get("target", "")).strip()) == target:
                    return {"tracker": dict(existing), "exists": True}

            next_id = max((int(t.get("id", 0) or 0) for t in self._trackers), default=0) + 1
            tracker = {
                "id": next_id,
                "target": target,
                "attempts_total": max(0, int(attempts)),
                "attempts_used": 0,
                "run_attempts": {},
                "retries_exhausted": False,
                "interval_seconds": max(5, int(interval_seconds)),
                "active": True,
                "auto_rerun": bool(auto_rerun),
                "last_detail": "",
                "last_error": "",
                "last_action": "created",
                "last_updated": 0.0,
                "next_check_ts": 0.0,
                "ignore_jobs": ignore,
                "repo": "",
                "pr_number": 0,
                "pr_title": "",
                "pr_base_title": "",
                "is_backport": False,
                "backport_target": "",
                "backport_source_pr": 0,
                "jobs": [],
                "approvals": 0,
                "labels": [],
            }
            self._prime_tracker_from_cache(tracker)
            self._trackers.append(tracker)
            self._save_trackers()
        return {"tracker": tracker}

    async def _tracker_add_targets(
        self,
        targets_text: str,
        attempts: int = 2,
        interval_seconds: int = 60,
        auto_rerun: bool = True,
        ignore_jobs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        text = str(targets_text or "").strip()
        if not text:
            raise JSONRPCError(self.INVALID_PARAMS, "targets_text is required")

        resolved = text
        if text.startswith("#"):
            maybe = _resolve_session_ref(text)
            if not maybe:
                raise JSONRPCError(self.INVALID_PARAMS, f"Unable to resolve session ref: {text}")
            resolved = maybe

        urls: list[str] = []
        seen: set[str] = set()

        for entry in _collect_structured_entries(resolved):
            u = str(entry.url).strip()
            if not u:
                continue
            m = _URL_RE.search(u)
            if not m or m.group("kind") != "pull":
                continue
            if u not in seen:
                seen.add(u)
                urls.append(u)

        for m in _URL_RE.finditer(resolved):
            if m.group("kind") != "pull":
                continue
            u = m.group(0)
            if u not in seen:
                seen.add(u)
                urls.append(u)

        if not urls:
            raise JSONRPCError(self.INVALID_PARAMS, "No PR targets found in input")

        created: list[dict[str, Any]] = []
        for url in urls:
            created.append(
                (
                    await self._tracker_add(
                        target=url,
                        attempts=attempts,
                        interval_seconds=interval_seconds,
                        auto_rerun=auto_rerun,
                        ignore_jobs=ignore_jobs,
                    )
                )["tracker"]
            )
        return {"added": len(created), "trackers": created}

    async def _tracker_remove(self, tracker_id: int) -> dict[str, Any]:
        with self._tracker_lock:
            original = len(self._trackers)
            self._trackers = [t for t in self._trackers if int(t.get("id", 0) or 0) != int(tracker_id)]
            removed = original - len(self._trackers)
            if removed:
                self._save_trackers()
        return {"removed": bool(removed), "tracker_id": int(tracker_id)}

    async def _tracker_add_attempts(self, tracker_id: int, delta: int = 1) -> dict[str, Any]:
        with self._tracker_lock:
            for tracker in self._trackers:
                if int(tracker.get("id", 0) or 0) == int(tracker_id):
                    tracker["attempts_total"] = max(0, int(tracker.get("attempts_total", 0) or 0) + int(delta))
                    tracker["last_action"] = f"attempts-adjusted:{delta}"
                    self._save_trackers()
                    if int(delta) > 0:
                        # New retries available — poll immediately.
                        self.force_refresh_sync(int(tracker_id))
                    return {"tracker": dict(tracker)}
        raise JSONRPCError(self.INVALID_PARAMS, f"tracker id {tracker_id} not found")

    async def _tracker_set_active(self, tracker_id: int, active: bool) -> dict[str, Any]:
        with self._tracker_lock:
            for tracker in self._trackers:
                if int(tracker.get("id", 0) or 0) == int(tracker_id):
                    tracker["active"] = bool(active)
                    tracker["last_action"] = "resumed" if active else "paused"
                    self._save_trackers()
                    return {"tracker": dict(tracker)}
        raise JSONRPCError(self.INVALID_PARAMS, f"tracker id {tracker_id} not found")

    async def _tracker_update_branch(self, tracker_id: int) -> dict[str, Any]:
        """Update a tracked PR's branch with its base branch.

        The live GitHub call runs in a worker thread to keep the event loop
        responsive. Returns ``{"ok": bool, "error": str}``; a failed update is
        surfaced as a normal result (not a JSON-RPC error) so the UI can show
        the message inline.
        """
        result = await asyncio.to_thread(self.update_branch_tracker_sync, int(tracker_id))
        if result.get("error") == "tracker not found":
            raise JSONRPCError(self.INVALID_PARAMS, f"tracker id {tracker_id} not found")
        return result

    async def _tracker_update(
        self,
        tracker_id: int,
        attempts_total: Optional[int] = None,
        interval_seconds: Optional[int] = None,
        auto_rerun: Optional[bool] = None,
        ignore_jobs: Optional[list[str]] = None,
        reset_attempts: Optional[bool] = None,
    ) -> dict[str, Any]:
        with self._tracker_lock:
            for tracker in self._trackers:
                if int(tracker.get("id", 0) or 0) == int(tracker_id):
                    if attempts_total is not None:
                        tracker["attempts_total"] = max(0, int(attempts_total))
                    if interval_seconds is not None:
                        tracker["interval_seconds"] = max(5, int(interval_seconds))
                    if auto_rerun is not None:
                        tracker["auto_rerun"] = bool(auto_rerun)
                    if ignore_jobs is not None:
                        tracker["ignore_jobs"] = [
                            str(x).strip()
                            for x in ignore_jobs
                            if str(x).strip()
                        ]
                        # Recompute status immediately so the change is reflected
                        # without waiting for the next poll cycle.
                        self._recompute_detail(tracker)
                    if reset_attempts:
                        tracker["attempts_used"] = 0
                        tracker["run_attempts"] = {}
                        tracker["retries_exhausted"] = False
                    tracker["last_action"] = "settings-updated"
                    self._save_trackers()
                    # Trigger an immediate poll whenever the change could unlock
                    # new reruns: attempts_total raised, attempts reset, or
                    # auto_rerun re-enabled.
                    should_refresh = (
                        (attempts_total is not None and int(attempts_total) > 0)
                        or bool(reset_attempts)
                        or (auto_rerun is True)
                    )
                    if should_refresh:
                        self.force_refresh_sync(int(tracker_id))
                    return {"tracker": dict(tracker)}
        raise JSONRPCError(self.INVALID_PARAMS, f"tracker id {tracker_id} not found")

    async def _repo_config_get(self, repo: str) -> dict[str, Any]:
        repo = str(repo or "").strip()
        cfg = self._repo_configs.get(repo, {})
        return {
            "repo": repo,
            "ignore_jobs": list(cfg.get("ignore_jobs", [])),
            "required_reviews": int(cfg.get("required_reviews", 0) or 0),
            "required_labels": list(cfg.get("required_labels", [])),
            "backport_ignore_jobs": list(cfg.get("backport_ignore_jobs", [])),
            "backport_required_reviews": int(cfg.get("backport_required_reviews", 0) or 0),
            "backport_required_labels": list(cfg.get("backport_required_labels", [])),
        }

    async def _repo_config_set(
        self,
        repo: str,
        ignore_jobs: Optional[list[str]] = None,
        required_reviews: Optional[int] = None,
        required_labels: Optional[list[str]] = None,
        backport_ignore_jobs: Optional[list[str]] = None,
        backport_required_reviews: Optional[int] = None,
        backport_required_labels: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        repo = str(repo or "").strip()
        if not repo:
            raise JSONRPCError(self.INVALID_PARAMS, "repo is required")
        cfg = dict(self._repo_configs.get(repo, {}))
        if ignore_jobs is not None:
            cfg["ignore_jobs"] = [str(x).strip() for x in ignore_jobs if str(x).strip()]
        if required_reviews is not None:
            cfg["required_reviews"] = max(0, int(required_reviews))
        if required_labels is not None:
            cfg["required_labels"] = [str(x).strip() for x in required_labels if str(x).strip()]
        if backport_ignore_jobs is not None:
            cfg["backport_ignore_jobs"] = [str(x).strip() for x in backport_ignore_jobs if str(x).strip()]
        if backport_required_reviews is not None:
            cfg["backport_required_reviews"] = max(0, int(backport_required_reviews))
        if backport_required_labels is not None:
            cfg["backport_required_labels"] = [str(x).strip() for x in backport_required_labels if str(x).strip()]
        self._repo_configs[repo] = cfg
        self._save_repo_configs()
        # Recompute last_detail for every tracker in this repo so the change
        # takes effect immediately without waiting for the next poll cycle.
        with self._tracker_lock:
            for tracker in self._trackers:
                if str(tracker.get("repo", "")).strip() == repo:
                    if self._recompute_detail(tracker):
                        tracker["last_action"] = "status-recomputed"
            self._save_trackers()
        return {
            "repo": repo,
            "ignore_jobs": list(cfg.get("ignore_jobs", [])),
            "required_reviews": int(cfg.get("required_reviews", 0) or 0),
            "required_labels": list(cfg.get("required_labels", [])),
            "backport_ignore_jobs": list(cfg.get("backport_ignore_jobs", [])),
            "backport_required_reviews": int(cfg.get("backport_required_reviews", 0) or 0),
            "backport_required_labels": list(cfg.get("backport_required_labels", [])),
        }

    async def _repo_log_filter_get(self, repo: str) -> dict[str, Any]:
        """Return the raw failed-CI log-extraction DSL text for *repo*."""
        repo = str(repo or "").strip()
        text = await asyncio.to_thread(_get_repo_log_filter, repo)
        return {"repo": repo, "log_filter": text}

    async def _repo_log_filter_set(self, repo: str, text: str) -> dict[str, Any]:
        """Persist the log-extraction DSL for *repo* (validates it first)."""
        repo = str(repo or "").strip()
        if not repo:
            raise JSONRPCError(self.INVALID_PARAMS, "repo is required")
        # Validate the DSL before saving so a bad regex surfaces to the caller.
        try:
            _parse_log_filter(text or "")
        except Exception as exc:
            raise JSONRPCError(self.INVALID_PARAMS, f"invalid log filter: {exc}")
        await asyncio.to_thread(_set_repo_log_filter, repo, text or "")
        # Drop cached excerpts so the next fetch re-greps with the new config.
        self._log_excerpt_cache.clear()
        self._record_event(f"log filter updated for {repo}")
        return {"repo": repo, "log_filter": text or ""}

    async def _repo_log_filter_get_structured(self, repo: str) -> dict[str, Any]:
        """Return the log filter as a form-friendly struct (+ raw text).

        Reads inline (tiny local file) so opening the config modal never waits on
        the thread pool, even while log downloads are in flight.
        """
        repo = str(repo or "").strip()
        text = _get_repo_log_filter(repo)
        return {"repo": repo, "struct": _log_filter_to_struct(text), "text": text}

    async def _repo_log_filter_set_structured(self, repo: str, struct: Any) -> dict[str, Any]:
        """Persist a log filter supplied as a form struct (serialized + validated)."""
        repo = str(repo or "").strip()
        if not repo:
            raise JSONRPCError(self.INVALID_PARAMS, "repo is required")
        text = _struct_to_log_filter(struct)
        try:
            _parse_log_filter(text)
        except Exception as exc:
            raise JSONRPCError(self.INVALID_PARAMS, f"invalid log filter: {exc}")
        await asyncio.to_thread(_set_repo_log_filter, repo, text)
        self._log_excerpt_cache.clear()
        self._record_event(f"log filter updated for {repo}")
        return {"repo": repo, "text": text, "struct": _log_filter_to_struct(text)}

    async def _log_filter_preview(self, struct: Any, job_names: Any = None) -> dict[str, Any]:
        """Validate a draft filter struct and report which job names each rule matches."""
        return _cli_log_filter_preview(struct, job_names or [])

    async def _log_filter_parse(self, text: str) -> dict[str, Any]:
        """Parse imported DSL text into a form struct (for the web import flow)."""
        empty = {"global": {"pattern": "", "before": 0, "after": 0}, "jobs": []}
        try:
            return {"ok": True, "error": "", "struct": _log_filter_to_struct(text or "")}
        except Exception as exc:
            return {"ok": False, "error": str(getattr(exc, "message", exc)), "struct": empty}

    def _compute_log_excerpt(
        self,
        repo: str,
        run_id: int,
        log_filter_text: str,
    ) -> dict[str, Any]:
        """Blocking: download the failed-job log for *run_id* and grep it.

        Walks the failing jobs in log_filter priority order and returns the first
        one whose grep actually produces matching lines — i.e. if a job's pattern
        finds nothing, it falls through to the next job. If none match, the first
        job's (empty) result is returned so the UI shows "(no matching lines)".
        Runs off the event loop via to_thread.
        """
        log_filter = _parse_log_filter(log_filter_text)
        gh = self._gh or (Github(self.token) if self.token else None)
        if gh is None:
            return {"run_id": run_id, "error": "no GitHub token configured", "lines": []}
        try:
            run = gh.get_repo(repo).get_workflow_run(int(run_id))
        except Exception as exc:
            return {"run_id": run_id, "error": f"cannot load run: {exc}", "lines": []}
        try:
            failed_jobs = _collect_failed_jobs(run)
        except Exception as exc:
            return {"run_id": run_id, "error": f"cannot load jobs: {exc}", "lines": []}
        if not failed_jobs:
            return {"run_id": run_id, "run_name": getattr(run, "name", ""), "lines": [],
                    "error": "no failed jobs"}
        # Selectors are a preference, not a hard filter: _select_failed_jobs falls
        # back to all failed jobs when none match, so this is non-empty here.
        selected = _select_failed_jobs(failed_jobs, log_filter)
        if not selected:
            return {"run_id": run_id, "run_name": getattr(run, "name", ""), "lines": [],
                    "error": "no failed jobs"}

        run_name = str(getattr(run, "name", "") or "")
        max_lines = 500
        # Cap how many job logs we download per call when falling through, so a run
        # with many same-named jobs (e.g. dozens of busted runners) can't trigger an
        # unbounded download burst.
        max_jobs = 10
        first_result: Optional[dict[str, Any]] = None
        for job, spec in selected[:max_jobs]:
            job_name = str(getattr(job, "name", "") or "")
            try:
                blob = _download_binary(_job_logs_url(job), self.token or "")
                lines: list[str] = []
                for _name, log_text in _decode_log_archive(blob):
                    lines.extend(_extract_log_lines(log_text, spec, color=False))
            except Exception as exc:
                if first_result is None:
                    first_result = {"run_id": run_id, "run_name": run_name,
                                    "job_name": job_name, "error": f"cannot fetch log: {exc}", "lines": []}
                continue
            result = {
                "run_id": run_id,
                "run_name": run_name,
                "job_name": job_name,
                "lines": lines[:max_lines],
                "truncated": len(lines) > max_lines,
            }
            if lines:
                return result  # this job's pattern matched — show it
            if first_result is None:
                first_result = result  # remember the first job in case nothing matches
        # Nothing matched any selected job's pattern — surface the first job so the
        # UI shows it with "(no matching lines)" rather than an error.
        return first_result if first_result is not None else {
            "run_id": run_id, "run_name": run_name, "lines": [], "error": "no failed jobs"}

    async def _tracker_log_excerpt(
        self,
        tracker_id: int,
        run_id: Optional[int] = None,
    ) -> dict[str, Any]:
        """Lazily fetch + grep the failed-CI log for a tracker's run.

        When *run_id* is omitted, the first failing run recorded on the tracker is
        used.  Results are cached by (run_id, log_filter text).
        """
        with self._tracker_lock:
            tracker = next(
                (t for t in self._trackers if int(t.get("id", 0) or 0) == int(tracker_id)),
                None,
            )
            if tracker is None:
                raise JSONRPCError(self.INVALID_PARAMS, f"tracker {tracker_id} not found")
            repo = str(tracker.get("repo", "") or "").strip()
            jobs = list(tracker.get("jobs") or [])

        if run_id is None:
            failing = next(
                (
                    j for j in jobs
                    if str(j.get("conclusion", "") or "").lower() in _RETRY_CONCLUSIONS
                ),
                None,
            )
            if failing is None:
                return {"run_id": None, "lines": [], "error": "no failing run on this tracker"}
            run_id = int(failing.get("id", 0) or 0)
        run_id = int(run_id)

        # Tiny local file read — keep it inline so it never queues behind a slow
        # log download in the shared thread pool (which would stall other RPCs,
        # e.g. the repo-config modal).
        log_filter_text = _get_repo_log_filter(repo)
        cache_key = (run_id, log_filter_text)
        cached = self._log_excerpt_cache.get(cache_key)
        if cached is not None:
            return cached

        # Bound concurrent downloads so a burst of auto-loads (one per failing PR)
        # can't saturate the thread pool / connection pool and freeze the UI.
        if self._log_excerpt_sem is None:
            self._log_excerpt_sem = asyncio.Semaphore(2)
        async with self._log_excerpt_sem:
            # Re-check the cache: another request may have fetched this meanwhile.
            cached = self._log_excerpt_cache.get(cache_key)
            if cached is not None:
                return cached
            result = await asyncio.to_thread(
                self._compute_log_excerpt, repo, run_id, log_filter_text
            )
        # Only cache successful fetches; let errors be retried.
        if not result.get("error"):
            self._log_excerpt_cache[cache_key] = result
        return result

    async def _tracker_refresh(self, tracker_id: Optional[int] = None) -> dict[str, Any]:
        with self._tracker_lock:
            selected = [
                tracker
                for tracker in self._trackers
                if tracker_id is None or int(tracker.get("id", 0) or 0) == int(tracker_id)
            ]

        for tracker in selected:
            try:
                await asyncio.to_thread(self._update_tracker, tracker)
            except Exception:
                logger.exception("Tracker %s manual refresh raised", tracker.get("id"))
                tracker["last_error"] = "internal error"
                tracker["last_action"] = "update-failed"
            tracker["next_check_ts"] = time.time() + max(
                5, int(tracker.get("interval_seconds", 60) or 60)
            )

        if selected:
            with self._tracker_lock:
                self._save_trackers()
        return {"refreshed": len(selected)}

    async def _clear_cache(self) -> dict[str, int]:
        """Clear the entire cache"""
        try:
            prs = self._cache.get("prs", {})
            count = len(prs) if isinstance(prs, dict) else 0
            self._cache["prs"] = {}
            self._save_cache()
            logger.info("Cleared cache: %d entries removed", count)
            return {"removed_count": count}
        except Exception as e:
            raise JSONRPCError(self.CACHE_ERROR, "Failed to clear cache: %s" % e)

    async def _list_sessions(self) -> list[dict[str, Any]]:
        """List saved sessions"""
        try:
            sessions = self._load_sessions()
            return [
                {
                    "index": i,
                    "timestamp": s.get("ts"),
                    "input": s.get("input", ""),
                }
                for i, s in enumerate(sessions)
            ]
        except Exception as e:
            raise JSONRPCError(self.CACHE_ERROR, f"Failed to list sessions: {e}")

    async def _resolve_session(self, ref: str) -> dict[str, Any]:
        """Resolve session reference (#last or #N)"""
        try:
            sessions = self._load_sessions()
            if not sessions:
                raise JSONRPCError(self.CACHE_ERROR, "No sessions found")

            # #last → last session
            if ref == "#last" or ref == "last":
                session = sessions[-1]
            else:
                # #N → Nth session (0-indexed)
                try:
                    idx = int(ref.lstrip("#"))
                    session = sessions[idx]
                except (ValueError, IndexError):
                    raise JSONRPCError(self.INVALID_PARAMS, f"Invalid session ref: {ref}")

            return {
                "timestamp": session.get("ts"),
                "input": session.get("input", ""),
                "targets": session.get("targets", []),
            }
        except JSONRPCError:
            raise
        except Exception as e:
            raise JSONRPCError(self.CACHE_ERROR, f"Failed to resolve session: {e}")

    async def _push_run_status(
        self,
        run_id: int,
        status: str,
        conclusion: Optional[str] = None,
        workflow_name: str = "",
    ) -> dict[str, str]:
        """Push workflow run status update"""
        try:
            # Store run status in cache under a runs key
            runs = self._cache.setdefault("runs", {})
            if not isinstance(runs, dict):
                self._cache["runs"] = {}
                runs = self._cache["runs"]

            runs[str(run_id)] = {
                "ts": time.time(),
                "status": status,
                "conclusion": conclusion,
                "workflow_name": workflow_name,
            }
            self._save_cache()
            logger.info("Pushed run status for %s: %s", run_id, status)
            return {"ack": True, "run_id": str(run_id)}
        except Exception as e:
            logger.error("Error pushing run status: %s", e)
            raise JSONRPCError(self.CACHE_ERROR, "Failed to push run status: %s" % e)

    async def _request_device_code(self) -> dict[str, Any]:
        """Request a device code from GitHub for device authorization flow."""
        try:
            client_id = _get_device_flow_client_id()
            resp = await asyncio.to_thread(_cli_request_device_code, client_id)
            return resp
        except Exception as e:
            logger.error("Error requesting device code: %s", e)
            raise JSONRPCError(self.INTERNAL_ERROR, "Failed to request device code: %s" % e)

    async def _poll_device_token(self, device_code: str) -> str:
        """Poll GitHub once for the access token granted after device code approval."""
        client_id = _get_device_flow_client_id()
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
            logger.error("Error polling device token: %s", exc)
            raise JSONRPCError(self.INTERNAL_ERROR, f"Device authorization network error: {exc.reason or exc}")

        data = json.loads(raw)
        if not isinstance(data, dict):
            raise JSONRPCError(self.INTERNAL_ERROR, "Unexpected device-token response from GitHub.")

        token = data.get("access_token")
        if isinstance(token, str) and token.strip():
            login = await asyncio.to_thread(_apply_token, self, token.strip())
            return login

        error = data.get("error")
        if error == "authorization_pending":
            raise JSONRPCError(self.INTERNAL_ERROR, "authorization_pending")
        if error == "slow_down":
            raise JSONRPCError(self.INTERNAL_ERROR, "slow_down")
        if error == "expired_token":
            raise JSONRPCError(self.INTERNAL_ERROR, "The device authorization expired. Run ghelper auth again.")

        description = data.get("error_description") or (str(error) if error else "unknown error")
        raise JSONRPCError(self.INTERNAL_ERROR, f"GitHub device flow failed: {description}")

    def _success_response(self, req_id: Optional[str], result: Any) -> dict[str, Any]:
        """Build JSON-RPC success response"""
        return {
            "jsonrpc": "2.0",
            "result": result,
            "id": req_id,
        }

    def _error_response(
        self,
        req_id: Optional[str],
        code: int,
        message: str,
        data: Any = None,
    ) -> dict[str, Any]:
        """Build JSON-RPC error response"""
        resp = {
            "jsonrpc": "2.0",
            "error": {
                "code": code,
                "message": message,
            },
            "id": req_id,
        }
        if data is not None:
            resp["error"]["data"] = data
        return resp


@web.middleware
async def cors_middleware(request: web.Request, handler: Any) -> web.Response:
    is_options = request.method == "OPTIONS"
    try:
        if is_options:
            response = web.Response()
        else:
            response = await handler(request)
    except web.HTTPException as exc:
        if is_options and exc.status == 405:
            response = web.Response()
        else:
            response = exc
            
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

def create_app(server: JSONRPCServer) -> web.Application:
    """Create aiohttp application"""
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_post("/rpc", server.handle_rpc)

    def _auth_status_payload() -> dict[str, Any]:
        return {
            "status": "ok",
            "authenticated": bool(server.token),
            "has_token": bool(server.token),
            "login": server.user_login,
        }

    async def auth(request: web.Request) -> web.Response:
        code = str(request.query.get("code", "") or "").strip()
        token = str(request.query.get("token", "") or "").strip()

        if not code and not token:
            return web.FileResponse(_WEB_DIR / "pages" / "auth.html")

        if code:
            token = await asyncio.to_thread(_exchange_oauth_code, code)
        if not token:
            return web.json_response(
                {
                    "status": "error",
                    "message": "Provide ?code=... for OAuth exchange or ?token=... for PAT compatibility.",
                },
                status=400,
            )

        try:
            login = await asyncio.to_thread(_apply_token, server, token)
        except Exception as exc:
            return web.json_response(
                {"status": "error", "message": str(exc)},
                status=400,
            )

        return web.json_response(
            {
                "status": "ok",
                "login": login,
                "has_token": True,
            }
        )

    async def auth_status(request: web.Request) -> web.Response:
        return web.json_response(_auth_status_payload())

    async def auth_logout(request: web.Request) -> web.Response:
        if request.method != "POST":
            return web.json_response({"status": "error", "message": "POST required"}, status=405)
        _clear_token(server)
        return web.json_response({"status": "ok", "authenticated": False, "has_token": False, "login": None})

    async def web_ui(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_WEB_DIR / "pages" / "index.html")

    async def add_targets_ui(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_WEB_DIR / "pages" / "add-targets.html")

    async def debug_state(request: web.Request) -> web.Response:
        prs = server._cache.get("prs", {})
        runs = server._cache.get("runs", {})
        return web.json_response({
            "status": "ok",
            "cache_path": str(server.cache_path),
            "trackers_path": str(server.trackers_path),
            "prs_count": len(prs) if isinstance(prs, dict) else 0,
            "runs_count": len(runs) if isinstance(runs, dict) else 0,
            "trackers_count": len(server._trackers),
            "tracker_loop_running": bool(server._tracker_task and not server._tracker_task.done()),
            "has_token": bool(server.token),
            "user_login": server.user_login,
        })
    
    # Health check endpoint
    async def health(request):
        return web.json_response({"status": "ok"})

    app.router.add_get("/", web_ui)
    app.router.add_get("/add-targets", add_targets_ui)
    app.router.add_get("/auth", auth)
    app.router.add_get("/auth/status", auth_status)
    app.router.add_post("/auth/logout", auth_logout)
    app.router.add_get("/health", health)
    app.router.add_get("/debug/state", debug_state)
    app.router.add_static("/static", _WEB_DIR / "static")

    # Force browsers to revalidate static assets (e.g. common.js) on every load.
    # Without this aiohttp's add_static omits Cache-Control, so browsers apply
    # heuristic freshness and keep running a stale cached copy after an update —
    # which makes bug fixes in the JS appear to "still happen" until a hard reload.
    async def _no_cache_static(request, response):
        if request.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(_no_cache_static)
    return app


async def start_server(
    socket_path: Optional[str] = None,
    token: Optional[str] = None,
    port: int = 53210,
    host: str = "127.0.0.1",
    open_browser: bool = False,
) -> None:
    """
    Start JSON-RPC server on Unix socket or TCP.

    Args:
        socket_path: Unix socket path (e.g. ~/.ghelper-server.sock)
        token: GitHub token
        port: TCP port if using localhost
        host: TCP host if using localhost
        open_browser: Open the web UI once the TCP listener is bound.
    """
    server = JSONRPCServer(token=token)
    app = create_app(server)

    runner = web.AppRunner(app)
    await runner.setup()

    try:
        if socket_path:
            socket_path_obj = Path(socket_path).expanduser()
            if socket_path_obj.exists():
                socket_path_obj.unlink()

            site = web.UnixSite(runner, str(socket_path_obj))
            click.echo("JSON-RPC server listening on unix socket %s" % socket_path_obj, err=True)
            click.echo("Web UI unavailable for unix socket mode; use --port for browser debugging", err=True)
        else:
            site = web.TCPSite(runner, host, port)
            click.echo("JSON-RPC server listening on http://%s:%d" % (host, port), err=True)
            click.echo("Web operations UI: http://%s:%d/" % (host, port), err=True)
            click.echo("Advanced debug tools are available inside the UI and hidden by default.", err=True)
            click.echo(
                "Methods: pushStatus, getPRStatus, getAssignedPRs, getRunStatus, "
                "pushRunStatus, cacheInfo, clearCache, listSessions, resolveSession, listMethods, "
                "trackerList, trackerAdd, trackerRemove, trackerAddAttempts, trackerSetActive, trackerRefresh",
                err=True,
            )

        await site.start()
        await server.start_background_tasks()

        if open_browser and not socket_path:
            asyncio.get_event_loop().call_later(
                0.2, webbrowser.open, "http://%s:%d/" % (host, port), 2,
            )

        while True:
            await asyncio.sleep(3600)

    except KeyboardInterrupt:
        click.echo("\nShutting down...", err=True)
    finally:
        await server.stop_background_tasks()
        await runner.cleanup()
