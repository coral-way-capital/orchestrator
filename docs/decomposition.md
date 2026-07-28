# Deterministic epic decomposition

`decomposition.py` is the mutation boundary between untrusted model output and
GitHub. A plan is parsed as strict JSON, validated in full, coverage-checked,
topologically ordered, and only then passed to the publisher.

The child contract requires a bounded scope, 2-5 acceptance criteria, a 1-3 day
size, owner, dependencies, non-goals, and parent requirement coverage. Unknown
dependencies, dependency cycles, duplicate ids, uncovered requirements, and
non-shippable children fail before any GitHub call.

## State transitions

An invalid first submission remains pending with stable validation errors and
returns `retry`. An invalid second submission moves to `failed` with
`status=manual`, `manual_required=true`, and `failure_class=validation_failed`.
There are no additional automatic attempts. Per-parent file locks serialize
concurrent submissions, terminal replays return the already-durable result, and
short queue-file locks prevent concurrent webhook enqueues from being lost.

Publication starts only after complete validation. If a later GitHub create
fails, the publisher closes every child created in that attempt and records
rollback evidence before routing the parent to manual handling. A rollback
failure is surfaced as `rollback_complete=false`; it is never hidden as a
successful or ordinary failed decomposition. Each child body carries a
deterministic parent/child marker; after a process restart, the publisher reuses
an already-open marked issue instead of creating a duplicate.

## Historical RCA

The checked-in [baseline](decomposition-rca.json) accounts for the reported 50
failed and 14 completed decompositions. No per-failure evidence exists in this
repository, so all 50 causes remain explicitly unknown. Given a sanitized JSON
array of failure records, generate a reproducible report with:

```bash
python3 decomposition.py rca records.json --completed-count 14
```

Classification uses evidence-only categories and redacts token-like values.
Missing or ambiguous evidence always remains `unknown`.

## Two-week production observation

`build_production_observation` emits `status=pending` and no rate until 14 full
days have elapsed after deployment. After that window, it reports failed/manual
and completed counts, the computed failure rate, and whether the `<30%` target
was observed. Generate the report from exported queue outcomes with:

```bash
python3 decomposition.py observe outcomes.json \
  --deployed-at 2026-08-01T00:00:00Z \
  --as-of 2026-08-15T00:00:00Z
```

The baseline deliberately contains no fabricated production result; deployment
and the two-week measurement remain operational follow-up.
