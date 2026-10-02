# Maya software factory

Decision, 2026-10-02: Ivan authorized dedicated Hermes profiles, native Kanban
execution, and retiring Mission Control as the factory control plane. Ivan then
authorized full Mission Control retirement, including its historical web UI and
compatibility API.

`factory` owns deterministic GitHub intake and factory maintenance. Native board
`factory` owns claims, workspaces, retry history and implementer/reviewer handoff.
`factory-implementer` opens a PR; `factory-reviewer` checks the exact head and
requests changes or completes with evidence. PR Guardian retains merge authority.
The old JSON queue and metrics are archived historical records. The Mission
Control service, dashboard, API and PR-outcome collector are no longer running.

## Admission and execution

- `factory_bridge.py --sync` runs as a script-only Hermes job every two minutes.
  The Guardian and review-noise cleaner also move to script-only jobs in this
  profile, keeping their existing cadence. Guardian reports merges/errors and
  stays silent when idle. Signed GitHub issue deliveries use `--webhook`; repeated deliveries share one
  durable `github:OWNER/REPO#NUMBER` idempotency key, including archived cards.
- Existing assignee and label rules apply. Maya continues to exclude
  `coral-way-capital/visit-merida-chatbot`. Current GitHub state and open linked
  PRs are checked before admission; unavailable evidence stops admission.
- Native parent dependencies serialize a repository's issue workflows, including
  review. Two native workers may run globally, one per profile. Intake creates
  dedicated repository anchors under `~/.hermes/factory/repos`; native worktrees
  use fresh branches fetched from the actual default branch. Retries reuse work.
- Run limit: 90 minutes. Native `max_retries=1` is a failure threshold: the first
  failed attempt blocks the card for review; it does not grant an automatic retry.
  Retry the existing card/worktree explicitly after resolving the cause. Native
  crash/heartbeat recovery owns execution. Long waits belong in blocked/scheduled cards.
- Code tasks use native repository completion contracts: required CI must pass
  on the exact PR head. A repository with no required checks remains blocked;
  configure an appropriate repository gate explicitly rather than weakening the
  card contract. Review/merge evidence does not imply client acceptance.

## Operations

Use `/home/deploy/.local/bin/hermes` over SSH (not on the default SSH PATH):

```sh
hermes kanban --board factory list
hermes kanban --board factory show TASK
hermes kanban --board factory runs TASK
hermes kanban --board factory log TASK
hermes -p factory cron list
hermes -p factory chat
```

Install the bundled `skills/devops/sdlc-review` in the reviewer profile: native
review dispatch requires it even when profiles are created with `--no-skills`.

The single existing gateway dispatches the allowlisted factory profiles and ticks
their cron stores. No second gateway/daemon is needed. Profile histories and
memories are separate; profiles share the OS account and provider credential
fallback, so they are not security sandboxes.

Mission Control is fully retired on Maya. `cwc-issue-webhook.service` is stopped,
masked and removed from boot startup; port 8646 has no listener. The former
`orchestrator.coralwaycapital.com` dashboard/API hostname returns HTTP 410.
The old default-profile dispatcher, cleaner and Guardian cron definitions were
removed after their factory-profile replacements became active. The legacy
dispatcher script and service files were privately archived.

Native Hermes issue/decomposer webhook routes remain script-only intake, and the
two-minute GitHub sync covers issue admission independently of legacy hooks.
Existing disabled review routes stay disabled. Legacy PR-outcome collection has
stopped; its database is history, not live telemetry. No native browser dashboard
has been installed; use the native CLI/board.

Weekly Company Scorecard and Weekly Portfolio Review now use the existing
versioned portfolio manifest and their other approved sources. They no longer
query the retired API. The company collector already supports this mode and
reports its Mission Control source as `not provided`; no business outcome is
inferred from engineering activity.

The checkout at `~/.hermes/issue-queue` remains the source location for the active
`factory_bridge.py`, eligibility and portfolio policy modules. Keep it and its
historical data; no Mission Control server is needed by those script imports.

Historical pending/failed cards import blocked, completed cards import done.
Imports never start an agent, assert new validation, or acquire repository lanes.
Reusing an imported card requires an explicit operator retry after checking
eligibility, assigning a managed worktree and restoring a repository contract.

## Validation and rollback

Run `python -m unittest -v test_factory test_dispatch_workspace test_workspace_prompts`.
Six focused checks pass on Maya, covering admission/error behavior, authenticated
HTTP cutover, and workspace isolation. Ivan accepted this validation for the
migration merge and Maya cutover on 2026-10-02 ([CQ-001](../docs/client-questions.md));
GitHub CI was not run because the existing OAuth logins cannot publish workflows.
On Maya's Hermes Python the same command also exercises real native
SQLite deduplication, dependency ordering, review handoff and failed acceptance.
A separate local-only canary verifies actual profile inference and handoff; it
must never create a GitHub issue/PR, send a message or alter a client repository.

Retirement was verified on Maya: masked/inactive service, closed legacy port,
HTTP 410 for the old HTTPS dashboard and API, healthy native gateway, and a
manifest-only scorecard rendered privately without vault writes or delivery.
The existing six focused tests and native profile canaries validated migration;
retirement itself changes deployment configuration and report instructions.

Private migration snapshots are under `~/.hermes/backups/factory-native-20261002/`;
full-retirement snapshots are under `~/.hermes/backups/mission-control-retired-20261002/`.
They include private service/config data and must never enter Git or the vault.
Restoration requires explicit operator intent: pause intake and drain native
workers, then selectively restore the retired service/routes/jobs. Preserve
unrelated proxy changes and never run both dispatchers together.
