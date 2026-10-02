# Maya software factory

Decision, 2026-10-02: Ivan authorized dedicated Hermes profiles, native Kanban
execution, and retiring Mission Control as the factory control plane.

`factory` owns deterministic GitHub intake and factory maintenance. Native board
`factory` owns claims, workspaces, retry history and implementer/reviewer handoff.
`factory-implementer` opens a PR; `factory-reviewer` checks the exact head and
requests changes or completes with evidence. PR Guardian retains merge authority.
The old JSON queue and metrics remain historical records, not another live queue.

## Admission and execution

- `factory_bridge.py --sync` runs as a script-only Hermes job every two minutes.
  Signed GitHub issue deliveries use `--webhook`; repeated deliveries share one
  durable `github:OWNER/REPO#NUMBER` idempotency key, including archived cards.
- Existing assignee and label rules apply. Maya continues to exclude
  `coral-way-capital/visit-merida-chatbot`. Current GitHub state and open linked
  PRs are checked before admission; unavailable evidence stops admission.
- Native parent dependencies serialize a repository's issue workflows, including
  review. Two native workers may run globally, one per profile. Intake creates
  dedicated repository anchors under `~/.hermes/factory/repos`; native worktrees
  use fresh branches fetched from the actual default branch. Retries reuse work.
- Run limit: 90 minutes; automatic retry limit: one. Native crash/heartbeat
  recovery owns execution. Long external waits belong in blocked/scheduled cards.
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

The single existing gateway dispatches the allowlisted factory profiles and ticks
their cron stores. No second gateway/daemon is needed. Profile histories and
memories are separate; profiles share the OS account and provider credential
fallback, so they are not security sandboxes.

Mission Control runs with `CWC_FACTORY_BACKEND=kanban`: signed issue intake is
forwarded to Hermes; old dispatch, finish, heartbeat, sync, retry and reaping
endpoints return HTTP 410. Read-only history and PR outcome ingestion remain
available for existing consumers. Its old dispatcher cron is paused. Existing
disabled review webhooks stay disabled; legacy issue/decomposer routes become
script-only native intake. Thus there is one active execution queue.

Historical pending/failed cards import blocked, completed cards import done.
Imports never start an agent, assert new validation, or acquire repository lanes.
Reusing an imported card requires an explicit operator retry after checking
eligibility, assigning a managed worktree and restoring a repository contract.

## Validation and rollback

Run `python -m unittest -v test_factory test_dispatch_workspace test_workspace_prompts`.
CI checks admission/error behavior, authenticated HTTP cutover, and workspace
isolation. On Maya's Hermes Python the same command also exercises real native
SQLite deduplication, dependency ordering, review handoff and failed acceptance.
A separate local-only canary verifies actual profile inference and handoff; it
must never create a GitHub issue/PR, send a message or alter a client repository.

Before cutover, privately back up config, subscriptions, cron records and the
legacy queue/database snapshots. Drain legacy workers and import once. Keep
those snapshots on Maya, never in Git. To roll back, pause native intake and
drain native workers first; only then remove the backend environment flag and
restore the old route/cron configuration. Never run both dispatchers together.
