#!/usr/bin/env python3
"""Historical backfill report for unresolved dispatch outcomes.

The audit (2026-07-27) found 50/126 dispatches without a terminal outcome because
reconciliation relied on a 2 MB gateway-log tail. This module produces a report
classifying every dispatch into exactly one of:

* ``resolved``   — a structured worker result or a gateway-log line exists, and
  the item is now in a terminal queue bucket (completed/failed).
* ``unknown``    — no terminal evidence could be found. The item stays where it
  is (in_progress) so it can be re-reconciled later. We NEVER infer success.

The report is evidence-backed: each classified dispatch carries the source(s)
that determined its outcome (``worker_result``, ``gateway_log``,
``session_file``, or ``none``).

Usage as a library::

    from backfill_report import build_report
    report = build_report(queue, gateway_outcomes=..., worker_results=...)
    print(report["summary"])

CLI::

    python3 backfill_report.py            # prints JSON summary
    python3 backfill_report.py --apply    # also ingest resolved outcomes
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker_results import (
    apply_outcome_to_queue,
    scan_session_files,
    validate_worker_result,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _item_dispatch_id(item: dict[str, Any]) -> str | None:
    dispatch_id = item.get("dispatch_id")
    if dispatch_id:
        return str(dispatch_id)
    telemetry = item.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("dispatch_id"):
        return str(telemetry["dispatch_id"])
    return None


def _item_outcome_from_gateway(outcomes: dict[str, dict[str, Any]], item: dict[str, Any]) -> dict[str, Any] | None:
    dispatch_id = _item_dispatch_id(item)
    if dispatch_id and dispatch_id in outcomes:
        return outcomes[dispatch_id]
    return None


def build_report(
    queue: dict[str, Any],
    *,
    gateway_outcomes: dict[str, dict[str, Any]] | None = None,
    worker_results_rows: list[dict[str, Any]] | None = None,
    session_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a resolved-vs-unknown backfill report from gathered evidence.

    Each parameter is optional; missing sources are simply not consulted. The
    function does NOT mutate ``queue`` — use :func:`apply_report` for that.
    """
    gateway_outcomes = gateway_outcomes or {}
    worker_results_rows = worker_results_rows or []
    session_candidates = session_candidates or []

    # Index worker results and session candidates by item_id (and dispatch_id).
    wr_by_item: dict[str, dict[str, Any]] = {}
    wr_by_dispatch: dict[str, dict[str, Any]] = {}
    for row in worker_results_rows:
        if row.get("status") not in ("completed", "failed"):
            continue
        wr_by_item.setdefault(row["item_id"], row)
        if row.get("dispatch_id"):
            wr_by_dispatch.setdefault(row["dispatch_id"], row)

    sess_by_item: dict[str, dict[str, Any]] = {}
    for cand in session_candidates:
        if cand.get("status") not in ("completed", "failed"):
            continue
        sess_by_item.setdefault(cand["item_id"], cand)

    resolved: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []

    in_progress = list(queue.get("in_progress", []))
    for item in in_progress:
        item_id = item.get("id")
        dispatch_id = _item_dispatch_id(item)
        entry = {
            "item_id": item_id,
            "repo": item.get("repo"),
            "issue_number": item.get("issue_number"),
            "dispatch_id": dispatch_id,
            "started_at": item.get("started_at") or item.get("agent_started_at"),
            "evidence_sources": [],
        }

        # 1. Structured worker result (preferred).
        wr = None
        if dispatch_id and dispatch_id in wr_by_dispatch:
            wr = wr_by_dispatch[dispatch_id]
        elif item_id in wr_by_item:
            wr = wr_by_item[item_id]
        if wr:
            entry["evidence_sources"].append("worker_result")
            evidence = wr.get("evidence")
            if evidence is None and wr.get("evidence_json"):
                try:
                    evidence = json.loads(wr["evidence_json"])
                except (TypeError, json.JSONDecodeError):
                    evidence = {"raw": str(wr["evidence_json"])[:500]}
            entry["evidence"] = evidence or {}
            entry["resolved_status"] = wr.get("status")
            entry["pr_number"] = wr.get("pr_number")
            entry["error_summary"] = wr.get("error_summary")
            resolved.append(entry)
            continue

        # 2. Gateway log scraping (existing reconciler fallback).
        gw = _item_outcome_from_gateway(gateway_outcomes, item)
        if gw and gw.get("status") in ("completed", "failed"):
            entry["evidence_sources"].append("gateway_log")
            entry["evidence"] = {
                "final_response": gw.get("final_response"),
                "delivery_id": gw.get("delivery_id"),
            }
            entry["resolved_status"] = gw.get("status")
            entry["pr_number"] = gw.get("pr_number")
            entry["error_summary"] = gw.get("error_summary")
            resolved.append(entry)
            continue

        # 3. Session-file fallback (trace bundle final.txt / meta.json).
        sess = sess_by_item.get(item_id)
        if sess:
            entry["evidence_sources"].append("session_file")
            entry["evidence"] = sess.get("evidence") or {}
            entry["resolved_status"] = sess.get("status")
            entry["pr_number"] = sess.get("pr_number")
            entry["error_summary"] = sess.get("error_summary")
            if entry["resolved_status"] == "unknown":
                unknown.append(entry)
            else:
                resolved.append(entry)
            continue

        # 4. No evidence at all → unknown. Never infer success.
        entry["evidence_sources"].append("none")
        entry["evidence"] = {"reason": "no terminal evidence found"}
        entry["resolved_status"] = "unknown"
        unknown.append(entry)

    total = len(resolved) + len(unknown)
    return {
        "generated_at": now_iso(),
        "summary": {
            "total_in_progress": len(in_progress),
            "resolved": len(resolved),
            "unknown": len(unknown),
            "unknown_pct": round((len(unknown) / total * 100), 2) if total else 0.0,
        },
        "resolved": resolved,
        "unknown": unknown,
        "production_metric": {
            "target": "<5% unknown worker outcomes over a rolling seven-day window",
            "status": "pending_post_deploy_observation",
        },
    }


def apply_report(report: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    """Apply the resolved entries of a backfill report to the queue in place.

    Returns a summary of how many items moved and to which buckets. Unknown
    entries are deliberately left untouched (preserved as unknown).
    """
    moved = {"completed": 0, "failed": 0, "unchanged": 0, "unknown_preserved": 0}
    for entry in report.get("resolved", []):
        result = {
            "version": 1,
            "dispatch_id": entry.get("dispatch_id"),
            "item_id": entry["item_id"],
            "status": entry["resolved_status"],
            "pr_number": entry.get("pr_number"),
            "error_summary": entry.get("error_summary"),
            "occurred_at": report.get("generated_at") or now_iso(),
        }
        result = validate_worker_result(result)
        summary = apply_outcome_to_queue(result, queue, mutate=True)
        if summary.get("applied"):
            bucket = "completed" if result["status"] == "completed" else "failed"
            moved[bucket] += 1
        else:
            moved["unchanged"] += 1
    moved["unknown_preserved"] = len(report.get("unknown", []))
    return moved


def main(apply: bool = False) -> None:
    import gateway_reconciler
    import queue as queue_mod
    import worker_results

    try:
        queue = queue_mod.load_queue()
    except Exception:
        queue = {"in_progress": [], "completed": [], "failed": [], "pending": []}

    gateway_outcomes = gateway_reconciler.scan_gateway_log()
    traces_root = Path.home() / ".hermes" / "issue-queue" / "traces"
    session_candidates = scan_session_files(traces_root, queue=queue)

    # Pull all worker_results rows from the ledger.
    wr_rows: list[dict[str, Any]] = []
    try:
        wr_rows = worker_results.list_results()
    except Exception:
        pass

    report = build_report(
        queue,
        gateway_outcomes=gateway_outcomes,
        worker_results_rows=wr_rows,
        session_candidates=session_candidates,
    )
    if apply:
        moved = apply_report(report, queue)
        queue_mod.save_queue(queue)
        report["applied"] = moved

    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Backfill report for unresolved dispatches")
    parser.add_argument("--apply", action="store_true", help="also ingest resolved outcomes into the queue")
    args = parser.parse_args()
    main(apply=args.apply)
