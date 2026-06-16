# ghelper

A headless Python CLI that polls GitHub Actions workflow runs and automatically re-runs failed jobs — designed to keep running on a server even when your laptop is off.

Pairs naturally with the **Backport Tracker** userscript: pipe its copy-summary output directly into `ghelper watch` for a fast workflow.

## Requirements

- Python 3.9+
- A GitHub personal access token (see [Authentication](#authentication))

## Installation

```bash
pip install -e .
```

This installs the `ghelper` command.

## Authentication

You need a GitHub token with permission to read and trigger workflow reruns.

The default sign-in flow uses GitHub's device authorization flow:

```
ghelper auth
```

prints a browser URL and short code, then polls GitHub until you approve the
device login.

If you prefer to keep using a PAT, pass `--pat` to fall back to the previous
token-based flow.

### Device flow

1. Run `ghelper auth`.
2. Open the URL printed by the command if your browser does not open automatically.
3. Enter the short code shown in the terminal.
4. Approve the login in GitHub.

Device-flow tokens use the `repo` scope and work for repos you can access,
including org-owned repos.

### PAT mode

Use `ghelper auth --pat` if you want to keep using a PAT.

Fine-grained PATs:

1. Follow the link printed by `ghelper auth --pat`.
2. Select the target repository.
3. Under **Repository permissions → Actions**, choose **Read and write**.
4. No other permissions are needed.

Classic PATs:

1. Follow the classic link printed by `ghelper auth --pat --classic`.
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
export GHELPER_GITHUB_CLIENT_SECRET=your_client_secret_here
```

You can still override the embedded client ID by setting
`GHELPER_GITHUB_CLIENT_ID` if needed.

---

## Command overview

| Command | What it does |
|---|---|
| `ghelper auth` | Start device flow by default; `--pat` keeps PAT mode |
| `ghelper watch [TARGETS]...` | Supervisor: TUI + auto-rerun + optional HTTP server |
| `ghelper ls` | List assigned PRs as Backport-Tracker-compatible markdown |
| `ghelper logs [TARGETS]...` | Print failed-job logs (`--grep` to filter) |
| `ghelper config show \| set \| clear` | Manage per-repo defaults at `~/.ghelper.json` |

`watch` is the canonical supervisor. The TUI is just an interface for an embedded server — the same tracker model is exposed over HTTP/JSON-RPC and the web UI when `--serve` is set, and you can drive it headlessly with `--serve --no-tui`.

---

## `ghelper watch`

```
ghelper watch [OPTIONS] [TARGETS]...
```

TARGETS can be any mix of:

| Format | Example |
|---|---|
| Actions run URL | `https://github.com/owner/repo/actions/runs/12345` |
| PR URL | `https://github.com/owner/repo/pull/456` |
| Bare run ID | `12345` (requires `-R owner/repo`) |
| Session ref | `#last`, `#3` — resume a previous invocation |

You can also pipe markdown summaries (e.g. from `ghelper ls` or Backport Tracker) on stdin. Targets can be added live from the TUI with `a`, or via the web UI / JSON-RPC when `--serve` is set.

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
exchange requires `GHELPER_GITHUB_CLIENT_ID` and
`GHELPER_GITHUB_CLIENT_SECRET` (or the legacy `GH_RERUNNER_*`
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
ghelper watch --serve --no-tui
```

starts the JSON-RPC server (`/rpc`) and web UI on `127.0.0.1:9999`. Trackers added via the web UI or RPC are persisted to `~/.ghelper-trackers.json` and reload across restarts.

---

## `ghelper ls`

Export PRs assigned to the authenticated user, in the same markdown format Backport Tracker emits. Useful as input to `watch`.

```
ghelper ls
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

## `ghelper logs`

Print failed workflow jobs and their logs. `--grep` keeps only matching lines plus adjacent context, with matched text highlighted.

```
ghelper logs --grep 'AssertionError|Traceback' --context 3 \
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

## `ghelper config`

Persistent per-repo defaults at `~/.ghelper.json`.

```bash
ghelper config show
ghelper config show -R owner/repo
ghelper config set -R owner/repo --ignore lint --ignore build-docs --required-label release-ready --required-reviews 1
ghelper config clear -R owner/repo
```

Saved fields per repo:

- `ignore_ci`: CI job-name substrings to ignore in rerun decisions
- `required_labels`: label substrings required on PR targets
- `required_reviews`: minimum approvals on PR targets (advisory in `watch`)

---

## Examples

```bash
# Export assigned PRs in backport-tracker format
ghelper ls

# Pipe to watch
ghelper ls | ghelper watch -n 5

# Watch a single Actions run
ghelper watch https://github.com/owner/repo/actions/runs/12345

# Watch all runs for a PR
ghelper watch https://github.com/owner/repo/pull/456

# Bare run ID — requires -R
ghelper watch -R owner/repo 12345

# Up to 5 retries, check every minute
ghelper watch -n 5 -i 60 https://github.com/owner/repo/pull/456

# Ignore two CI jobs
ghelper watch --ignore lint --ignore docs https://github.com/owner/repo/pull/456

# Watch all assigned PRs
ghelper watch -a

# Resume the previous session
ghelper watch #last

# Pipe backport-tracker "Copy summary" output
pbpaste | ghelper watch

# Inspect failed logs
ghelper logs --grep 'AssertionError|Traceback' --context 3 \
  https://github.com/owner/repo/pull/456

# Headless server mode (no TUI, web UI on localhost:9999)
ghelper watch --serve --no-tui
```

### Backport Tracker integration

In the Backport Tracker sidebar panel, click **Copy summary**, then:

```bash
pbpaste | ghelper watch -n 3
```

`ghelper watch` extracts all GitHub URLs from the markdown automatically and reads any `ignore_ci="..."` directive embedded in the comment header.

A pasted summary may also include a leading title line and status rows that have
no PR link yet, e.g.:

```
fix(plugins): oidc compliance of redirect_uri in different grant_types
[MISSING] next/3.10.x.x
[MISSING] next/3.11.x.x
[MERGED] next/3.14.x.x: https://github.com/Kong/kong-ee/pull/18741
[MERGED] ai-master: https://github.com/Kong/kong-ee/pull/18745
```

The title line is captured for display, rows with a PR URL become watch targets,
and `[MISSING]`-style rows (branches without a backport PR yet) are surfaced as
"nothing to track" instead of being treated as targets. The same parsing applies
in the web UI's **Add Targets** import box.

---

## Running on a server

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e /path/to/gh-helper
export GITHUB_TOKEN=ghp_...

# Headless: HTTP server + tracker engine, no TUI
nohup ghelper watch --serve --no-tui --host 0.0.0.0 --port 9999 \
  >> ~/ghelper.log 2>&1 &
```

Trackers persist in `~/.ghelper-trackers.json`, so the server can be restarted without losing state. Connect to the web UI at `http://<host>:9999/` or drive it via `POST /rpc`.
