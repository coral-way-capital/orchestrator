# Worker evaluations and feedback

`worker_evaluations.py` builds an idempotent evaluation registry from terminal
PR outcomes. It is a read-only consumer of the PR ledger: it never calls
GitHub, edits prompts, or changes model/task routing.

Each terminal PR has one evaluation covering merge state, explicit review
severity, fix-up ratio, exact time to merge, reopening/follow-up signals, and
human override. It also records exact queue-item, dispatch, structured-result,
and PR-ledger linkage. Review-cycle and review-delay metrics come from the PR
ledger; build and size remain explicitly `not_available` because that ledger
does not contain exact source data. Accepted business outcome is always
separate and `not_available`; merge is never treated as acceptance. Findings
retain exact GitHub review, commit, PR, issue, or comment URLs when the source
provides them. Missing source signals are stored as `not_available`; partial
evidence has an `evidence_gap`.

Routing dimensions are enriched from the exact dispatch telemetry row when its
dispatch, queue item, repository, and PR identities agree. Exact terminal
provider/model values take precedence over requested values; requested values
are used only when terminal adapter values are unavailable. Missing, invalid,
or unreadable telemetry is nonblocking and does not change PR evaluation
coverage.

Review severity is accepted only from an explicit
`[severity:critical|high|medium|low]` review-body tag. Fix-up ratio is:

```
commits after the first changes-requested review / all PR commits
```

If review bodies or commit history are absent, the registry does not infer
either value. Only the bounded severity tag and review/commit provenance are
persisted; review-body text is not copied into the ledger.

Evaluation refreshes are replay-safe. The current row is unchanged when only
the evaluation clock or input ordering changes. Material evidence revisions
are appended once to `worker_evaluation_history` by semantic hash before the
current projection changes.

Generate a deterministic weekly digest:

```bash
python3 worker_evaluations.py \
  --week-start 2026-07-21T00:00:00Z
```

The digest groups PRs whose exact terminal timestamp falls in the requested
week by prompt, model provider/model, and task class. A failure must repeat at
least twice to qualify. Ordering is count descending and then lexical, so ties
are stable. Rows with unavailable routing dimensions are counted as excluded
and cannot rank or produce a recommendation. It names at most one top failure
and proposes at most one unapplied system change.

## Routing approval boundary

Feedback is recommendations-only. `routing_gate()` always returns
`automatic_routing_enabled: false`, including after the observation period and
after an Ivan approval record. Automatic routing cannot be introduced until:

1. at least 30 days of observation have elapsed;
2. GitHub user `ivanacostarubio` has explicitly approved crossing the boundary
   in linked GitHub evidence at or after the eligibility instant; and
3. a later, separately reviewed code change adds an actual routing mechanism.

This issue starts the observation mechanism; it does not claim that the
30-day gate has elapsed or that approval exists. Malformed, premature, future,
unlinked, and other-actor approvals are rejected. Even a valid boundary record
cannot mutate routing, provider, model, prompt, weight, queue, or production
configuration.

## Acceptance evidence

| Criterion | Code | Deterministic test |
|---|---|---|
| 100% terminal PR evaluation coverage | `refresh_registry`, `refresh_from_pr_outcomes` | `test_terminal_fixture_coverage_and_evidence_provenance`, `test_pr_outcome_adapter_evaluates_every_terminal_row_without_inference` |
| Exact comments/commits or explicit unavailable state | evaluation evidence fields; `pr_outcomes.list_pull_events` | `test_terminal_fixture_coverage_and_evidence_provenance` |
| Weekly top repeated failure and proposed change | `build_weekly_digest` | `test_weekly_digest_is_stable_and_recommends_without_routing` |
| 30-day and Ivan approval boundary; no automatic mutation | `routing_gate` | `test_automatic_routing_requires_elapsed_gate_and_ivan_approval` |
