# Mission Control Portfolio API

Mission Control exposes a read-only, evidence-backed view of CWC engagement projects. Scoring is deterministic: the API computes from a versioned portfolio manifest and never infers client adoption, payment, or acceptance.

## Source contract

Default manifest:

`/home/deploy/apps/cwc-control-plane/portfolio/projects.json`

Override for local development or another deployment:

```bash
export CWC_PORTFOLIO_MANIFEST=/absolute/path/to/projects.json
```

The control-plane repository owns the schema, human-reviewed project records, and `scripts/verify_portfolio.py`. The CWC Obsidian vault remains the source of truth for client context and evidence notes.

## Endpoints

### `GET /api/portfolio`

Returns the complete ranked portfolio:

- policy and score weights
- WIP summary and limit violations
- scored projects in deterministic rank order
- score breakdown and dominant gap
- blocker ownership and decision dates
- evidence status and freshness
- copyable decision/advice brief

### `GET /api/portfolio/{project_id}`

Returns one enriched project record. Responds with `404` when the identifier is unknown.

### `GET /api/portfolio/{project_id}/brief`

Returns the bounded decision brief used by the dashboard's **Copy advice brief** action. It contains score, recommendation, accepted outcome, finish gate, blockers, and evidence summary. It must not contain credentials or secrets.

If the manifest is absent or invalid, portfolio endpoints respond with `503` and a safe error payload. Other Mission Control views continue to work.

## Score model

Ratings are integers from 0 to 5. Weighted dimensions total 100 points:

| Dimension | Weight |
|---|---:|
| Accepted outcome and adoption | 25 |
| Finishability | 20 |
| Commercial commitment | 15 |
| Outcome clarity | 15 |
| Blocker ownership | 10 |
| Evidence quality | 10 |
| Strategic compounding | 5 |

Action bands:

- 80–100: scale
- 65–79: finish
- 50–64: fix
- 35–49: escalate
- 0–34: pause

## Verification

```bash
python3 -m unittest -v test_portfolio.py test_portfolio_http.py test_portfolio_dashboard.py
python3 -m py_compile portfolio.py webhook_receiver.py
python3 - <<'PY' | node --check --input-type=module
from pathlib import Path
text = Path('dashboard/index.html').read_text()
print(text.split('<script type="module">', 1)[1].split('</script>', 1)[0])
PY
```

The Portfolio tab is the default Mission Control view and uses the CWC dark design system. The first release provides deterministic advice rather than an unbounded chat agent; a future conversational layer must follow the AI SDK Elements + AG-UI standard.