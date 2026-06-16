# gh-rerunner

A headless Python CLI that polls GitHub Actions workflow runs and automatically re-runs failed jobs — designed to keep running on a server even when your laptop is off.

Pairs naturally with the **Backport Tracker** userscript: pipe its copy-summary output directly into `gh-rerunner watch` for a fast workflow.

## Requirements

- Python 3.9+
- A GitHub personal access token (see [Authentication](#authentication))

## Installation

```bash
pip install -e .
```

This installs the `gh-rerunner` command.

## Authentication

You need a GitHub token with permission to read and trigger workflow reruns.

The default sign-in flow uses GitHub's device authorization flow:

```
gh-rerunner auth
```

prints a browser URL and short code, then polls GitHub until you approve the
device login.

If you prefer to keep using a PAT, pass `--pat` to fall back to the previous
token-based flow.

### Device flow

1. Run `gh-rerunner auth`.
2. Open the URL printed by the command if your browser does not open automatically.
3. Enter the short code shown in the terminal.
4. Approve the login in GitHub.

Device-flow tokens use the `repo` scope and work for repos you can access,
including org-owned repos.

### PAT mode

Use `gh-rerunner auth --pat` if you want to keep using a PAT.

Fine-grained PATs:

1. Follow the link printed by `gh-rerunner auth --pat`.
2. Select the target repository.
3. Under **Repository permissions → Actions**, choose **Read and write**.
4. No other permissions are needed.

Classic PATs:

1. Follow the classic link printed by `gh-rerunner auth --pat --classic`.
2. The `repo` scope will be pre-selected — that is sufficient.

> The `repo` scope grants write access to all repository resources, not just
> Actions. Use a fine-grained PAT if you want tighter access control.

### Providing the token

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

For device flow, the GitHub OAuth client ID is built in. You only need to set
the client secret for the `/auth?code=...` OAuth exchange path:

```bash
export GH_RERUNNER_GITHUB_CLIENT_SECRET=your_client_secret_here
```

You can still override the embedded client ID by setting
`GH_RERUNNER_GITHUB_CLIENT_ID` if needed.

---

## Command overview

| Command | What it does |
|---|---|
| `gh-rerunner auth` | Start device flow by default; `--pat` keeps PAT mode |
| `gh-rerunner watch [TARGETS]...` | Supervisor: TUI + auto-rerun + optional HTTP server |
| `gh-rerunner ls` | List assigned PRs as Backport-Tracker-compatible markdown |
| `gh-rerunner logs [TARGETS]...` | Print failed-job logs (`--grep` to filter) |
| `gh-rerunner config show \| set \| clear` | Manage per-repo defaults at `~/.gh-rerunner.json` |

`watch` is the canonical supervisor. The TUI is just an interface for an embedded server — the same tracker model is exposed over HTTP/JSON-RPC and the web UI when `--serve` is set, and you can drive it headlessly with `--serve --no-tui`.

---

## `gh-rerunner watch`

```
gh-rerunner watch [OPTIONS] [TARGETS]...
```

TARGETS can be any mix of:

| Format | Example |
|---|---|
| Actions run URL | `https://github.com/owner/repo/actions/runs/12345` |
| PR URL | `https://github.com/owner/repo/pull/456` |
| Bare run ID | `12345` (requires `-R owner/repo`) |
| Session ref | `#last`, `#3` — resume a previous invocation |

You can also pipe markdown summaries (e.g. from `gh-rerunner ls` or Backport Tracker) on stdin. Targets can be added live from the TUI with `a`, or via the web UI / JSON-RPC when `--serve` is set.

**Options:**

| Flag | Default | Description |
|---|---|---|
| `-t, --token` | `$GITHUB_TOKEN` | GitHub PAT (required) |
| `-R, --repo OWNER/REPO` | — | Required for bare run IDs |
| `-n, --retries N` | `3` | Maximum rerun attempts per run |
| `-i, --interval SECS` | `30` | Server-side polling interval |
| `--ignore JOB` | — | CI job substring to ignore; repeatable |
| `-a, --assigned` | off | Watch PRs assigned to the current user |
| `--filter REGEX` | — | Regex filter (with `--assigned`) |
| `--include-closed` | off | Include closed PRs (with `--assigned`) |
| `--include-drafts` | off | Include draft PRs (with `--assigned`) |
| `--serve` | off | Expose HTTP/JSON-RPC + web UI |
| `--host HOST` | `127.0.0.1` | HTTP bind host (with `--serve`) |
| `--port PORT` | `53210` | HTTP bind port (with `--serve`) |
| `--no-tui` | off | Skip the Rich dashboard (useful with `--serve`) |
| `--quiet` | off | Suppress streaming event lines (non-TTY mode) |

The embedded server also exposes `GET /auth`. Pass `?code=...` to exchange an
OAuth code for a token, or `?token=...` to set a PAT directly. The OAuth code
exchange requires `GH_RERUNNER_GITHUB_CLIENT_ID` and
`GH_RERUNNER_GITHUB_CLIENT_SECRET` (or the legacy `GH_RERUNNER_OAUTH_*`
names).

### TUI shortcuts (in `watch`)

| Key | Action |
|---|---|
| `Tab` | Cycle panes (targets → runs → jobs → logs) |
| `j`/`k` or `↑`/`↓` | Move selection / scroll |
| `←`/`→` or `PgUp`/`PgDn` | Page step within the current pane |
| `g`/`G` or `Home`/`End` | Jump to top / bottom |
| `Space` | Expand / collapse the selected aggregate group |
| `a` | Add a tracker via modal prompt |
| `d` | Remove the selected tracker |
| `r` | Force-refresh the selected tracker (all if not on targets) |
| `l` | Toggle the logs pane |
| `o`, `Enter` | Open the selected item in your browser |
| `Ctrl-C` | Exit |

### Headless / server mode

```bash
gh-rerunner watch --serve --no-tui
```

starts the JSON-RPC server (`/rpc`) and web UI on `127.0.0.1:9999`. Trackers added via the web UI or RPC are persisted to `~/.gh-rerunner-trackers.json` and reload across restarts.

---

## `gh-rerunner ls`

Export PRs assigned to the authenticated user, in the same markdown format Backport Tracker emits. Useful as input to `watch`.

```
gh-rerunner ls
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `-t, --token` | `$GITHUB_TOKEN` | GitHub PAT |
| `-R, --repo OWNER/REPO` | — | Optional repo scope |
| `--include-closed` | off | Include closed PRs |
| `--include-drafts` | off | Include draft PRs |
| `--filter REGEX` | — | Regex filter against branch/title/url/repo |

---

## `gh-rerunner logs`

Print failed workflow jobs and their logs. `--grep` keeps only matching lines plus adjacent context, with matched text highlighted.

```
gh-rerunner logs --grep 'AssertionError|Traceback' --context 3 \
  https://github.com/owner/repo/actions/runs/12345
```

**Options:**

| Flag | Default | Description |
|---|---|---|
| `-t, --token` | `$GITHUB_TOKEN` | GitHub PAT |
| `-R, --repo OWNER/REPO` | — | Required for bare run IDs |
| `--grep REGEX` | — | Print only matching lines (plus context) |
| `--context N` | `2` | Adjacent lines around each match |

You can pipe summary text containing GitHub URLs instead of listing TARGETS.

---

## `gh-rerunner config`

Persistent per-repo defaults at `~/.gh-rerunner.json`.

```bash
gh-rerunner config show
gh-rerunner config show -R owner/repo
gh-rerunner config set -R owner/repo --ignore lint --ignore build-docs --required-label release-ready --required-reviews 1
gh-rerunner config clear -R owner/repo
```

Saved fields per repo:

- `ignore_ci`: CI job-name substrings to ignore in rerun decisions
- `required_labels`: label substrings required on PR targets
- `required_reviews`: minimum approvals on PR targets (advisory in `watch`)

---

## Examples

```bash
# Export assigned PRs in backport-tracker format
gh-rerunner ls

# Pipe to watch
gh-rerunner ls | gh-rerunner watch -n 5

# Watch a single Actions run
gh-rerunner watch https://github.com/owner/repo/actions/runs/12345

# Watch all runs for a PR
gh-rerunner watch https://github.com/owner/repo/pull/456

# Bare run ID — requires -R
gh-rerunner watch -R owner/repo 12345

# Up to 5 retries, check every minute
gh-rerunner watch -n 5 -i 60 https://github.com/owner/repo/pull/456

# Ignore two CI jobs
gh-rerunner watch --ignore lint --ignore docs https://github.com/owner/repo/pull/456

# Watch all assigned PRs
gh-rerunner watch -a

# Resume the previous session
gh-rerunner watch #last

# Pipe backport-tracker "Copy summary" output
pbpaste | gh-rerunner watch

# Inspect failed logs
gh-rerunner logs --grep 'AssertionError|Traceback' --context 3 \
  https://github.com/owner/repo/pull/456

# Headless server mode (no TUI, web UI on localhost:9999)
gh-rerunner watch --serve --no-tui
```

### Backport Tracker integration

In the Backport Tracker sidebar panel, click **Copy summary**, then:

```bash
pbpaste | gh-rerunner watch -n 3
```

`gh-rerunner watch` extracts all GitHub URLs from the markdown automatically and reads any `ignore_ci="..."` directive embedded in the comment header.

---

## Running on a server

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e /path/to/gh-helper
export GITHUB_TOKEN=ghp_...

# Headless: HTTP server + tracker engine, no TUI
nohup gh-rerunner watch --serve --no-tui --host 0.0.0.0 --port 9999 \
  >> ~/rerunner.log 2>&1 &
```

Trackers persist in `~/.gh-rerunner-trackers.json`, so the server can be restarted without losing state. Connect to the web UI at `http://<host>:9999/` or drive it via `POST /rpc`.
