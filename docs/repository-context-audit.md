# Mission Control repository-context audit

Mission Control exposes the versioned repository-context audit produced by
`coral-way-capital/cwc-control-plane` as a read-only dashboard surface. The
control plane owns report generation and review. Mission Control never runs an
audit, edits repository context, creates findings, or performs remediation.

## Configuration

The report root defaults to:

`/home/deploy/apps/cwc-control-plane/repository-context`

Set `CWC_CONTEXT_AUDIT_REPORT_ROOT` to an absolute deployment-specific root.
`CWC_CONTEXT_AUDIT_STALE_DAYS` controls the stale indicator and defaults to 30.
An absent, unreadable, or invalid root produces a bounded `503` response and an
honest unavailable dashboard state; it does not affect queue endpoints.

Only these versioned control-plane inputs are accepted:

- `repositories.json`
- `baselines/YYYY-MM-DD.json`
- `deltas/YYYY-MM-DD[-label].json`

The newest dated report is displayed, with a delta preferred over a baseline
on the same date. Symlinked inputs, path escapes, noncanonical references,
unsupported schema versions, inconsistent deltas, nonempty `actions`, and
non-GitHub evidence URLs are rejected.

## API

`GET /api/context-audit` returns a whitelisted projection of schema-v1 report
data:

- report kind, observation age, and stale status
- inventory observation and immutable source revision
- overall coverage, average score, threshold finding count, and delta movement
- repository scores, seven subscores, audited revision, and score change
- internal threshold findings
- normalized, immutable GitHub evidence references

The response never includes report filenames, configured filesystem paths,
file contents, evidence messages, actions, credentials, or arbitrary URLs.
There are no mutation endpoints for this feature.

## Dashboard

The **Context audit** tab shows:

- overall repository coverage and source provenance
- score-below-60 and drop-over-10 findings
- score and delta filters
- per-repository scores and all seven subscores
- immutable evidence links
- explicit current, stale, and unavailable states

The surface is intentionally observational. Any proposed repository-context
change must be reviewed and performed through the control-plane workflow.

## Verification

All fixtures are synthetic, redacted, and created under temporary directories:

```bash
python3 -m unittest -v \
  test_context_audit_reports.py \
  test_context_audit_http.py \
  test_context_audit_dashboard.py
```
