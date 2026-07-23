# 👻 GhostMail

AI-powered Gmail management with digital identity shaping.

GhostMail tames a busy Gmail inbox: it sweeps new mail on a schedule, labels it
into a clean `GM/*` taxonomy, separates job-hunting **action mail** (interviews,
assessments, offers) from **noise** (job-alert digests, application receipts,
marketing dressed up as recruiting), stars what needs you, and drafts a daily
digest of open action items to your own inbox. A tiered LLM router keeps cost
near zero: deterministic rules first, cheap models only for the ambiguous few.

## Features

- **Batch Sorter** — scheduled inbox sweep; every processed email gets a
  `GM/Sorted` marker so "unsorted" is a clean, self-correcting query
- **Job Pipeline (capstone)** — deterministic Stage-1 classifier + optional
  Stage-2 LLM confirm; action items are starred, kept in inbox, optionally
  cross-linked to your JobAuto application records, and recorded for the digest
- **Daily Digest** — one draft email to yourself summarizing open action items,
  plus a Windows desktop notification; stale items auto-resolve after 14 days
- **Operator** — AI triage that sorts, labels, and prioritizes your inbox
- **Curator** — audits and shapes your digital identity (dry-run by default)
- **Archivist** — organizes email into a dynamic label hierarchy
- **Extras** — research/search over your mail, contact-intelligence CRM,
  political-email unsubscribe helper, expense extraction, MCP server

## Architecture

```
scheduler/                     Windows Task Scheduler registration + logged runner
src/ghostmail/
  batch_sorter.py              scheduled sweep: labels mail, captures action items
  job_classifier.py            deterministic Stage-1 job-email classifier (pure functions)
  action_triage.py             Stage-2 LLM confirm, action store, daily digest
  ai_engine.py                 model router (MiniMax / Kimi via OpenCode Zen, DeepSeek)
  gmail_gateway.py             Gmail API client: OAuth2, rate limiting, headless-safe auth
  database.py                  SQLite local cache
  config.py                    settings (GHOSTMAIL_* env vars, pydantic-settings)
  cli.py                       Typer CLI (`ghostmail ...`)
  mcp_server.py                optional FastAPI JSON-RPC server for research/search
  modules/                     operator, curator, archivist, research, contacts,
                               expense_tracker, political_unsub
tests/                         pytest: classifier rules, action-triage hygiene,
                               Gmail auth-failure handling
```

**Pipeline flow:** `batch_sorter` (every 3h) → `job_classifier` (Stage-1 rules)
→ `action_triage` (Stage-2 LLM confirm on the ambiguous handful, cross-link,
record) → daily digest draft + desktop notification (`scheduler/` registers the
Windows Task Scheduler tasks).

## Privacy & security model

- **Local-first:** email content stays on your machine; only metadata is cached
  in a local SQLite database
- **Sensitive-content guard:** mail matching keywords like "password", "ssn",
  "bank", "medical" is flagged for local-only processing
- **No telemetry:** the only external calls are to the Gmail API and the LLM
  providers *you* configure
- **Human-in-the-loop:** destructive actions (delete, send) require approval;
  shaping/organizing commands default to dry-run
- **Credentials stay local:** OAuth tokens, SQLite caches, action data, and logs
  live in `~/.ghostmail/data/` (outside this repo). `.env` is git-ignored; only
  `.env.example` placeholders are committed
- **Sanitized auth failures:** headless runs never print OAuth error bodies
  (which can carry token data) — static guidance and the exception type only

## Setup

### 1. Install

```bash
git clone <your-fork-url> ghostmail
cd ghostmail
pip install -e .
# or: pip install -r requirements.txt
# dev extras (pytest, ruff): pip install -e .[dev]
```

Requires **Python 3.11+**.

### 2. Get Gmail API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create a project and enable the **Gmail API**
3. **Credentials → Create Credentials → OAuth Client ID** (type: **Desktop app**)
4. Copy the Client ID and Client Secret

### 3. Configure environment

```bash
cp .env.example .env   # then fill in your values
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GHOSTMAIL_GMAIL_CLIENT_ID` | — | Gmail OAuth2 Client ID |
| `GHOSTMAIL_GMAIL_CLIENT_SECRET` | — | Gmail OAuth2 Client Secret |
| `GHOSTMAIL_SELF_EMAIL` | — | Your Gmail address — digest sender/recipient; self-sent mail is excluded from sweeps |
| `GHOSTMAIL_SELF_ALIASES` | `[]` | Extra addresses you send from (JSON list, e.g. `'["you@work.com"]'`) |
| `GHOSTMAIL_DATA_DIR` | `~/.ghostmail/data` | Local data directory (OAuth token, DB, action store, logs) |
| `GHOSTMAIL_OPENCODE_API_KEY` | — | OpenCode Zen key (MiniMax / Kimi free tier) |
| `GHOSTMAIL_DEEPSEEK_API_KEY` | — | DeepSeek key (Stage-2 confirm; `deepseek-chat`/`deepseek-reasoner`) |
| `GHOSTMAIL_JOBAUTO_JOBS_PATH` | — | Optional path to a JobAuto `jobs.json`; enables recruiter-mail cross-linking. Unset = cross-link disabled |

### 4. Authenticate & verify

```bash
ghostmail setup    # config check
ghostmail auth     # browser OAuth2 flow (interactive, one time)
ghostmail test     # LLM connectivity check
```

## Usage

```bash
ghostmail sync                          # cache recent email metadata locally
ghostmail triage --limit 10             # preview AI triage on 10 emails
ghostmail triage --limit 20 --auto      # auto-execute high-confidence actions
ghostmail curate --audit-only           # analyze your email profile (no changes)
ghostmail organize --dry-run            # preview label organization (no changes)
ghostmail research "OpenAI API"         # research a topic across your mail
ghostmail contacts sync                 # build the contact-intelligence CRM
ghostmail mcp-server --port 8765        # serve research/search over JSON-RPC
```

### Batch sorter (scheduled pipeline)

```bash
python -m ghostmail.batch_sorter --mode incremental --days 45 --limit 400 --no-archive
python -m ghostmail.batch_sorter --dry-run        # classify + report, zero writes
python -m ghostmail.action_triage --dry-run       # preview digest; no writes, no Gmail calls
```

**Dry-run behavior:** `--dry-run` is pure — no Gmail writes, no local state
writes, no notifications. It reports what *would* happen (labels, action items,
digest contents) and exits.

**Scheduled-task auth failure:** headless runs never launch a browser. If the
OAuth token is expired/revoked (`invalid_grant`), the run aborts *before any
Gmail write*, prints sanitized re-auth instructions (no token data), and exits
with **code 3**. Re-auth once interactively with
`python -m ghostmail.batch_sorter --mode incremental --days 1 --limit 1`.

### Windows Task Scheduler

```powershell
# From the repo root (registers \GhostMail\gm-sort and \GhostMail\gm-digest):
powershell -NoProfile -ExecutionPolicy Bypass -File scheduler\Register-GhostMailTasks.ps1

# Rollback:
powershell -NoProfile -ExecutionPolicy Bypass -File scheduler\Unregister-GhostMailTasks.ps1
```

Both scripts are location-independent: the project root defaults to the parent
of `scheduler\` (`$PSScriptRoot`), overridable with `-ProjectRoot`. The runner
uses `python` from `PATH` by default; override with `-PythonExe` or the
`GHOSTMAIL_PYTHON` env var. Logs land in `data\sched-logs\`.

## Customization

- **`batch_sorter.HEURISTIC`** is a plain rules list
  (`(field, pattern, bucket, reason)`) for non-job mail — add your own
  newsletter/financial/shopping/alumni domains and map buckets in
  `GMAIL_LABELS`.
- **`job_classifier`** sender lists (job-alert relays, assessment platforms,
  staffing agencies, marketing deny-list) are ordinary tuples — extend them as
  new senders show up in your inbox.

## Tests

```bash
pip install -e .[dev]
python -m pytest tests -q                   # full suite (no network, no Gmail)
```

The suite is hermetic: classifier rules run on synthetic fixtures, action-store
tests write only to pytest `tmp_path`, and auth-failure tests use temporary
credential paths. No test touches a real mailbox.

## Project status

Personal open-source project, shared as-is. It runs daily against one real
Gmail inbox (the maintainer's), but it is opinionated, Windows-first, and
lightly tested beyond the pipeline core — expect to tweak heuristics for your
own mail. Issues and PRs welcome; no SLAs.

## License

MIT — see [LICENSE](LICENSE).
