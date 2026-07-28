# AGENTS.md — CWC Mission Control / Issue Orchestrator

## What This Is

A dashboard and orchestration layer for GitHub issues owned by [Coral Way Capital (CWC)](https://github.com/coral-way-capital). Issues arrive via webhook or manual GitHub sync, appear on a kanban board, and can be dispatched to autonomous coding agents via the Hermes gateway.

**Stack:** Python 3 (stdlib + PyYAML), Preact + HTM SPA, SQLite, systemd, Hermes Agent (gateway).

## Architecture

```
                          ┌─────────────────────────────────┐
                          │     Mission Control Dashboard    │
                          │   dashboard/index.html (SPA)     │
                          │   kanban · issue detail · metrics │
                          └──────────┬──────────────────────┘
                                     │ REST API
                          ┌──────────▼──────────────────────┐
                          │    webhook_receiver.py :8646      │
                          │   webhooks · API · static files   │
                          └──┬─────┬──────┬──────────┬──────┘
                             │     │      │          │
                   ┌─────────┘     │      │          │
                   ▼               ▼      ▼          ▼
              queue.py       events.py   sync    prompts/
              (queue CRUD)  (event log)  (inc.)  (templates)
                   │
         ┌─────────┼──────────┐
         ▼         ▼          ▼
   enqueue()   dispatch()   sync_github_issues()
   classify    (pi -p)      (incremental)
   (S/M/L)

   dispatch() spawns Pi agent:
   cd <repo> && pi -p --no-session --append-system-prompt '...' < prompt.md
```

## Files

| File | Purpose |
|------|---------|
| `webhook_receiver.py` | HTTP server (port 8646). GitHub webhooks (POST), dashboard (GET), REST API (GET/PATCH/POST). Forwards large issues to Hermes gateway for decomposition. Spawns Pi agents on dispatch. |
| `queue.py` | Queue CRUD + GitHub sync. Manages `queue.json` with lists: `pending`, `in_progress`, `completed`, `failed`. Incremental sync via `?since=` with per-repo timestamps. |
| `events.py` | SQLite event log at `events.db`. Structured log of all events (enqueue, claim, complete, fail, dispatch, sync, guard triggers). Powers metrics, timeline, and stats. |
| `orchestrator_check.py` | Cron script. Reads queue, determines which issues to dispatch, outputs context for agents. Max 2 concurrent workers, 1 per repo. |
| `liveness.py` | Worker liveness classification (live/stale/dead) and safe stale-worker reaping. Two-signal model: heartbeat staleness + process check. Idempotent, audited, preserves recovery manifest. |
| `dashboard/index.html` | Single-file Preact SPA. Kanban board with clickable cards, issue detail modal, agent dispatch with prompt selector, activity timeline, metrics, guards, decompose tree. Dark/light mode. |
| `prompts/` | Prompt template directory. Each `.md` file is a template with YAML frontmatter and `{{variable}}` placeholders. Drop new files to add prompt options. |
| `cwc-issue-webhook.service` | systemd unit file for the webhook receiver. |

### Prompt Templates

| File | Name | What it does |
|------|------|-------------|
| `prompts/default.md` | Default — Implement & PR | Read issue, implement solution, open PR |
| `prompts/explore-plan.md` | Explore & Plan | Read codebase, post implementation plan as GitHub comment |
| `prompts/fix-bug.md` | Fix Bug | Reproduce → diagnose → fix → PR with test evidence |
| `prompts/review-harden.md` | Review & Harden | Review code, find issues, push improvements |

Add new templates by creating a `.md` file in `prompts/`:

```markdown
---
name: My Custom Prompt
description: What this prompt does
---
Your template with {{title}}, {{repo}}, {{local_path}},
{{html_url}}, {{body}}, {{issue_number}} variables.
```

## Runtime Environment

- **Host:** `100.102.201.26` (Tailscale) — server name `maya`
- **User:** `deploy`
- **Working directory:** `/home/deploy/.hermes/issue-queue/`
- **Port:** 8646 (bound to Tailscale IP)
- **Service:** `cwc-issue-webhook.service` (systemd, enabled, auto-restart)
- **Agent:** `pi` (Pi coding agent, `/usr/local/bin/pi`)

### Data Files (on server, gitignored)

| Path | What |
|------|------|
| `~/.hermes/issue-queue/queue.json` | Main issue queue |
| `~/.hermes/issue-queue/decompose-queue.json` | Epic decomposition queue |
| `~/.hermes/issue-queue/sync-state.json` | Per-repo sync timestamps (enables incremental sync) |
| `~/.hermes/issue-queue/events.db` | SQLite event log |
| `~/.hermes/issue-queue/webhook-secret` | HMAC secret shared with GitHub |
| `~/.hermes/issue-queue/prompts/*.md` | Prompt templates (tracked in git) |

### Environment Variables (systemd service)

| Variable | Purpose |
|----------|---------|
| `PORT` | Server port (default 8646) |
| `GITHUB_TOKEN` | GitHub PAT for `gh` CLI (used by sync, issue fetch, agent) |
| `CWC_CONTEXT_AUDIT_REPORT_ROOT` | Read-only control-plane repository-context report root |
| `CWC_CONTEXT_AUDIT_STALE_DAYS` | Age in days before a context report is marked stale (default 30) |

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
  "error": null,
  "closed_via": null,
  "agent_pid": null,
  "agent_log": null,
  "agent_prompt": null,
  "agent_started_at": null
}
```

### Agent Fields

| Field | Set when | Purpose |
|-------|----------|---------|
| `agent_pid` | Dispatch | Process ID of the `pi` background process |
| `agent_log` | Dispatch | Path to the agent's log file |
| `agent_prompt` | Dispatch | ID of the prompt template used |
| `agent_started_at` | Dispatch | ISO timestamp when agent was spawned |
| `closed_via` | Sync prune | Set to `"sync_prune"` when auto-closed by sync |

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
| `/api/issue?repo=X&number=N` | Full GitHub issue data + comments |
| `/api/prompts` | List of available prompt templates |
| `/api/context-audit` | Redacted read-only projection of the newest canonical repository-context baseline/delta |
| `/api/health` | Health check with queue counts |
| `/health` | Simple health check |

### PATCH

| Path | Action |
|------|--------|
| `/api/queue/prioritize/<id>` | Move pending item to top (position 0) |
| `/api/queue/move-down/<id>` | Move pending item down one position |
| `/api/queue/move-to-bottom/<id>` | Move pending item to the very end of the queue |
| `/api/queue/remove/<id>` | Remove an item, or safely recover with `{"requeue":true}`; add `"dry_run":true` for a zero-mutation preview |
| `/api/queue/retry/<id>` | Move failed item back to pending |

### POST

| Path | Body | Action |
|------|------|--------|
| `/` | GitHub webhook payload | Receives `issues` events, classifies size, enqueues or routes to decomposition |
| `/api/dispatch` | `{"item_id": "...", "prompt_id": "..."}` | Claim item, render prompt, spawn `pi -p` agent in background |
| `/api/heartbeat` | `{"worker_id"\|"item_id", "phase", "progress", "message"}` | Worker heartbeat: stamp `last_heartbeat_at`, store phase/progress. Required for liveness reaping (issue #16) |

### Worker Liveness & Reaping (issue #16)

Mission Control distinguishes **live**, **stale**, and **dead** workers using a two-signal model implemented in `liveness.py`:

1. **Heartbeat staleness** — every active worker should emit a heartbeat (`POST /api/heartbeat`) at least every 60 s. A worker whose heartbeat is older than `HEARTBEAT_TIMEOUT_SECONDS` (60 s) is *stale*.
2. **Process/session liveness** — a stale worker is only *dead* when a process probe (`kill -0 <pid>`) confirms the PID is gone. Workers without a PID rely on heartbeat alone: stale = dead-eligible only after confirmation.

**A live process is never reaped by time alone.** Only workers that are *both* stale (heartbeat) AND confirmed dead (process check) are reaped.

Reaping is **idempotent** and **fully audited**:
- Each reaped worker gets a JSON recovery manifest at `<traces>/<item>/reaper_recovery.json` preserving branch, worktree, log paths, and recovery instructions.
- A structured `worker.reaped` event is logged via `events.log_event`.
- Re-reaping an already-removed worker is a no-op.

Workers receive a scoped bearer token at dispatch and emit authenticated
heartbeats with phase/progress so the dashboard can show what long-running jobs
are doing:
```json
POST /api/heartbeat
Authorization: Bearer <worker-scoped token>
{"item_id": "coral-way-capital/audit-agent#42", "phase": "writing tests", "progress": 0.7}
```

### Sync Response Format

`GET /api/sync` returns structured per-repo results:

```json
{
  "repos": {
    "coral-way-capital/audit-agent": {
      "added": ["coral-way-capital/audit-agent#42"],
      "closed": ["coral-way-capital/audit-agent#38"],
      "unchanged": 5,
      "errors": []
    }
  },
  "totals": { "added": 1, "closed": 1, "unchanged": 5 }
}
```

## Issue Classification

Issues are classified as `small`, `medium`, or `large` based on body heuristics:
- Body length (>5000 chars = large)
- Section count (`## ` headings)
- List item count
- Presence of acceptance criteria + work items
- Mentions of pilots/phases

**Large issues** → decompose queue → forwarded to Hermes gateway for epic decomposition into child issues.

## GitHub Sync (Incremental)

The sync mechanism uses per-repo timestamps stored in `sync-state.json`:

| Scenario | GitHub API call | What happens |
|----------|----------------|-------------|
| **First sync** (no timestamp) | `?state=open&per_page=100` | Fetch all open issues. Queue items not in open set are individually verified and pruned if closed. |
| **Subsequent sync** | `?state=all&per_page=100&since=<timestamp>` | Only issues updated since last sync. Typically 0-5 results. Open → enqueue, closed → prune. |

Helper functions:
- `verify_issue_closed(repo, number)` — Single-issue `gh api` state check
- `auto_close_item(item_id, source)` — Move item from any list → `completed` with `closed_via` flag

## Guards (Recursion Prevention)

- `epic-child` label → skip (child of decomposition)
- Title pattern `^\[Parent #\d+\]` → skip (decomposition child)
- Open linked PRs → skip (already being worked on)
- Skip labels: `question`, `discussion`, `wontfix`, `duplicate`, `invalid`, `docs`
- Decompose queue cap: 10 pending max

## Repo → Local Path Map

Used by `orchestrator_check.py` and the dispatch API to tell agents where code lives:

```
coral-way-capital/audit-agent    → /home/deploy/apps/audit-agent
coral-way-capital/eckhart        → /home/deploy/apps/eckhart
coral-way-capital/rsm-monitor    → /home/deploy/apps/eckhart
coral-way-capital/zenna-crm      → /home/deploy/apps/zenna-crm
coral-way-capital/inmuebles      → /home/deploy/apps/inmuebles
coral-way-capital/infrastructure → /home/deploy/apps/infrastructure
coral-way-capital/tasks-cli      → /home/deploy/apps/tasks-cli
coral-way-capital/agent-configs  → /home/deploy/apps/agent-configs
coral-way-capital/sre            → /home/deploy/apps/sre
coral-way-capital/website        → /var/www/coralwaycapital
coral-way-capital/client-status  → /home/deploy/apps/client-status
coral-way-capital/diffusionZones → /home/deploy/apps/diffusionZones
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

# Test sync manually
python3 queue.py sync
python3 queue.py sync coral-way-capital/audit-agent

# Test prompts API
curl http://100.102.201.26:8646/api/prompts
```

## Conventions

- **Minimal dependencies** — Python stdlib + PyYAML. `gh` CLI for GitHub API, `hermes` (Hermes agent venv) for agent spawning.
- **Single-file frontend** — `dashboard/index.html` is a self-contained Preact SPA (ESM imports from esm.sh, no build step)
- **Queue as JSON file** — `queue.json` is the source of truth, read/written on every operation. Not thread-safe by design (single-process, systemd-managed)
- **Event sourcing** — `events.py` logs every state transition for metrics and audit
- **Webhook secret** — HMAC-SHA256 verification via `~/.hermes/issue-queue/webhook-secret`
- **Max 100 completed items** retained in queue to prevent unbounded growth
- **Prompts are files** — Drop/edit `.md` files in `prompts/` to customize agent behavior. No restart needed.
