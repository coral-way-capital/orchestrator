# AGENTS.md — CWC Mission Control / Issue Orchestrator

## What This Is

An automated issue-to-PR pipeline for GitHub repositories owned by [Coral Way Capital (CWC)](https://github.com/coral-way-capital). When a GitHub issue is opened, a webhook enqueues it, a cron-based orchestrator dispatches Pi coding agents to resolve it, and the dashboard tracks everything in real time.

**Stack:** Python 3 (stdlib only — no pip dependencies), Preact + HTM SPA, SQLite, systemd.

## Architecture

```
GitHub Issue ──webhook──▶ webhook_receiver.py (port 8646)
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              classify     enqueue()    log_event()
              (S/M/L)      queue.py     events.py
                    │
          ┌─────────┴──────────┐
          ▼                    ▼
   Small/Medium           Large issue
   → pending queue        → decompose queue → Hermes gateway → epic decomposition
          │
          ▼
   orchestrator_check.py (cron) → dispatches Pi agents
          │
          ▼
   Agent opens PR → complete() → moves to completed
```

## Files

| File | Purpose |
|------|---------|
| `webhook_receiver.py` | HTTP server (port 8646). Handles GitHub webhooks (POST), serves dashboard (GET), exposes REST API (GET/PATCH). Also forwards large issues to Hermes gateway for decomposition. |
| `queue.py` | Queue CRUD. Manages `queue.json` with lists: `pending`, `in_progress`, `completed`, `failed`. Each item tracks repo, issue number, title, body, author, labels, html_url, timestamps, PR number. |
| `events.py` | SQLite event log at `~/.hermes/issue-queue/events.db`. Structured log of all events (enqueue, claim, complete, fail, guard triggers, etc.). Powers dashboard metrics, timeline, and stats. |
| `orchestrator_check.py` | Cron script. Reads queue, determines which issues to dispatch, outputs context for Pi agents. Max 2 concurrent workers, 1 per repo. |
| `dashboard/index.html` | Single-file Preact SPA. Vertical kanban board, activity timeline, metrics, guards, decompose tree. Dark/light mode. |
| `cwc-issue-webhook.service` | systemd unit file for the webhook receiver. |

## Runtime Environment

- **Host:** `100.102.201.26` (Tailscale) — server name `maya`
- **User:** `deploy`
- **Working directory:** `/home/deploy/.hermes/issue-queue/`
- **Port:** 8646
- **Service:** `cwc-issue-webhook.service` (systemd, enabled, auto-restart)

### Data Files (on server, gitignored)

| Path | What |
|------|------|
| `~/.hermes/issue-queue/queue.json` | Main issue queue |
| `~/.hermes/issue-queue/decompose-queue.json` | Epic decomposition queue |
| `~/.hermes/issue-queue/sync-state.json` | Per-repo sync timestamps (enables incremental sync) |
| `~/.hermes/issue-queue/events.db` | SQLite event log |
| `~/.hermes/issue-queue/webhook-secret` | HMAC secret shared with GitHub |

## Queue Item Schema

```json
{
  "id": "coral-way-capital/audit-agent#42",
  "repo": "coral-way-capital/audit-agent",
  "issue_number": 42,
  "title": "Fix auth token refresh",
  "body": "...",
  "author": "ivan",
  "labels": ["bug", "p0"],
  "html_url": "https://github.com/coral-way-capital/audit-agent/issues/42",
  "enqueued_at": "2026-05-17T17:30:00Z",
  "started_at": null,
  "completed_at": null,
  "pr_number": null,
  "error": null
}
```

## API Endpoints

### GET

| Path | Returns |
|------|---------|
| `/api/queue` | Full queue JSON |
| `/api/queue-enriched` | Queue with cycle times and event counts per item |
| `/api/decompose-queue` | Decompose queue JSON |
| `/api/events?limit=50` | Recent events (params: `type`, `repo`, `item_id`, `since`, `limit`, `offset`) |
| `/api/stats` | Aggregate metrics (cycle times, hourly activity, event breakdown by type/repo, guard triggers) |
| `/api/decompose-tree` | Parent→child tree from decomposition events |
| `/api/repos` | Sorted list of all repos in the queue |
| `/api/sync?repo=<repo>` | Incremental sync: add new open issues, prune closed ones. Returns per-repo breakdown |
| `/api/health` | Health check with queue counts |
| `/health` | Simple health check |

### PATCH

| Path | Action |
|------|--------|
| `/api/queue/prioritize/<id>` | Move pending item to top (position 0) |
| `/api/queue/move-down/<id>` | Move pending item down one position |
| `/api/queue/move-to-bottom/<id>` | Move pending item to the very end of the queue |
| `/api/queue/remove/<id>` | Remove item from any queue list |
| `/api/queue/retry/<id>` | Move failed item back to pending |

### POST

| Path | Action |
|------|--------|
| `/` (GitHub webhook) | Receives `issues` events, classifies size, enqueues or routes to decomposition |

## Issue Classification

Issues are classified as `small`, `medium`, or `large` based on body heuristics:
- Body length (>5000 chars = large)
- Section count (`## ` headings)
- List item count
- Presence of acceptance criteria + work items
- Mentions of pilots/phases

**Large issues** → decompose queue → forwarded to Hermes gateway for epic decomposition into child issues.

## Guards (Recursion Prevention)

- `epic-child` label → skip (child of decomposition)
- Title pattern `^\[Parent #\d+\]` → skip (decomposition child)
- Open linked PRs → skip (already being worked on)
- Skip labels: `question`, `discussion`, `wontfix`, `duplicate`, `invalid`, `docs`
- Decompose queue cap: 10 pending max

## Repo → Local Path Map

Used by `orchestrator_check.py` to tell agents where code lives:

```
coral-way-capital/audit-agent   → /home/deploy/apps/audit-agent
coral-way-capital/eckhart       → /home/deploy/apps/eckhart
coral-way-capital/zenna-crm     → /home/deploy/apps/zenna-crm
coral-way-capital/inmuebles     → /home/deploy/apps/inmuebles
coral-way-capital/infrastructure→ /home/deploy/apps/infrastructure
coral-way-capital/tasks-cli     → /home/deploy/apps/tasks-cli
coral-way-capital/agent-configs → /home/deploy/apps/agent-configs
coral-way-capital/sre           → /home/deploy/apps/sre
coral-way-capital/website       → /var/www/coralwaycapital
```

## Deployment

```bash
# From local machine
ssh deploy@100.102.201.26
cd ~/.hermes/issue-queue
git pull
sudo systemctl restart cwc-issue-webhook

# Check status
sudo systemctl status cwc-issue-webhook

# View logs
sudo journalctl -u cwc-issue-webhook -f
```

## Conventions

- **Zero pip dependencies** — everything uses Python stdlib (`http.server`, `sqlite3`, `json`, `subprocess` for `gh` CLI)
- **Single-file frontend** — `dashboard/index.html` is a self-contained Preact SPA (ESM imports from esm.sh, no build step)
- **Queue as JSON file** — `queue.json` is the source of truth, read/written on every operation. Not thread-safe by design (single-process, systemd-managed)
- **Event sourcing** — `events.py` logs every state transition for metrics and audit
- **Webhook secret** — HMAC-SHA256 verification via `~/.hermes/issue-queue/webhook-secret`
- **Max 100 completed items** retained in queue to prevent unbounded growth
