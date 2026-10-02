# Client questions and decisions

Canonical decision inbox, following Ivan's client-question template. Valid states:
`Pendiente`, `Respondida`, `Descartada`. Keep closed decisions as history.

## Pending

### CQ-001 — CI publishing permission for the native factory migration

- Estado: Pendiente
- Registered: 2026-10-02
- Respondent: Ivan
- Target date: 2026-10-02
- Related work: [native Hermes factory migration PR #16](https://github.com/coral-way-capital/orchestrator/pull/16)
- Blocks: migration PR merge and production cutover, not implementation or local/native validation

GitHub rejected the prepared workflow because the existing Maya and local OAuth
logins lack `workflow` scope. Six focused checks pass on Maya, including real
native Kanban and HTTP behavior. PR #15 was separately authorized and merged
using Maya validation; that exception is not assumed for this new PR.

Question: enable workflow permission and publish/run GitHub CI, or explicitly
accept the passing Maya evidence for merging this migration without GitHub CI?

Recommendation: enable workflow permission so the regression checks remain
automatic for subsequent changes. Until resolved, native profile smoke testing,
review and migration preparation may continue; automatic factory cutover waits.

Response/evidence: awaiting Ivan's answer in the Codex conversation on 2026-10-02.

## Answered

## Discarded
