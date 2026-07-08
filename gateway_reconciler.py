#!/usr/bin/env python3
"""Reconcile Mission Control queue state from Hermes gateway final responses.

Gateway webhook workers run asynchronously and may return final text (``PR #123``
or ``FAILED: ...``) only in ``~/.hermes/logs/gateway.log``. If Mission Control
misses that callback, items stay stuck in ``in_progress``. This module is the
small deterministic repair loop used by the no-agent dispatcher before selecting
new work.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path.home() / ".hermes" / "issue-queue"
DEFAULT_GATEWAY_LOG = Path.home() / ".hermes" / "logs" / "gateway.log"

RESPONSE_RE = re.compile(
    r"Response for (?P<dispatch_id>webhook:cwc-issue-dispatch:(?P<delivery_id>[^:\s]+)):\s*(?P<response>.*)$"
)
PR_RE = re.compile(r"\bPR\s*#(?P<pr>\d+)\b", re.IGNORECASE)
SELF_IMPROVEMENT_MARKERS = (
    "Self-improvement review:",
    "💾 Self-improvement review:",
)
NON_FINAL_MARKERS = (
    "⚠️ **Dangerous command requires approval:**",
    "Dangerous command requires approval",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_gateway_response_line(line: str) -> dict[str, Any] | None:
    """Parse one gateway log line into a terminal worker outcome.

    Returns ``None`` for non-final webhook chatter. We intentionally ignore
    post-response skill patch messages because those arrive on the same webhook
    chat after the actual worker final answer.
    """
    m = RESPONSE_RE.search(line or "")
    if not m:
        return None
    response = (m.group("response") or "").strip()
    if not response:
        return None
    if any(marker in response for marker in SELF_IMPROVEMENT_MARKERS):
        return None
    if any(marker in response for marker in NON_FINAL_MARKERS):
        return None
    dispatch_id = m.group("dispatch_id")
    delivery_id = m.group("delivery_id")

    if response.startswith("FAILED:"):
        return {
            "dispatch_id": dispatch_id,
            "delivery_id": delivery_id,
            "status": "failed",
            "error_summary": response[:1000],
            "final_response": response,
        }

    pr_match = PR_RE.search(response)
    if pr_match:
        return {
            "dispatch_id": dispatch_id,
            "delivery_id": delivery_id,
            "status": "completed",
            "pr_number": int(pr_match.group("pr")),
            "final_response": f"PR #{int(pr_match.group('pr'))}",
        }

    return {
        "dispatch_id": dispatch_id,
        "delivery_id": delivery_id,
        "status": "non_contract",
        "error_summary": f"Worker finished without contract output: {response[:900]}",
        "final_response": response,
    }


def scan_gateway_log(log_path: str | Path = DEFAULT_GATEWAY_LOG, max_bytes: int = 2_000_000) -> dict[str, dict[str, Any]]:
    """Return latest terminal outcome by dispatch_id from the gateway log."""
    path = Path(log_path)
    if not path.exists() or not path.is_file():
        return {}
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        else:
            f.seek(0)
        text = f.read().decode("utf-8", errors="replace")

    outcomes: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        parsed = parse_gateway_response_line(line)
        if parsed:
            outcomes[parsed["dispatch_id"]] = parsed
    return outcomes


def _item_dispatch_id(item: dict[str, Any]) -> str | None:
    dispatch_id = item.get("dispatch_id")
    if dispatch_id:
        return dispatch_id
    telemetry = item.get("telemetry") or {}
    if isinstance(telemetry, dict):
        return telemetry.get("dispatch_id")
    return None


def _write_trace_final(item: dict[str, Any], outcome: dict[str, Any]) -> None:
    try:
        trace_dir = BASE_DIR / "traces" / re.sub(r"[^A-Za-z0-9_.-]+", "_", item.get("id", "unknown")).strip("._")
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "final.txt").write_text(outcome.get("final_response") or outcome.get("error_summary") or "", encoding="utf-8")
        meta_path = trace_dir / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update({
            "reconciled_at": now_iso(),
            "reconciled_status": outcome.get("status"),
            "reconciled_pr_number": outcome.get("pr_number"),
            "reconciled_error_summary": outcome.get("error_summary"),
        })
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _update_agent_trace(item: dict[str, Any], outcome: dict[str, Any]) -> None:
    try:
        from agent_traces import get_latest_trace_for_item, upsert_trace

        existing = get_latest_trace_for_item(item["id"])
        fields = {
            "item_id": item["id"],
            "repo": item.get("repo"),
            "issue_number": item.get("issue_number"),
            "session_id": item.get("session_id"),
            "dispatch_id": _item_dispatch_id(item),
            "status": outcome.get("status"),
            "finished_at": now_iso(),
            "pr_number": outcome.get("pr_number"),
            "exit_reason": "gateway_final_response",
            "error_summary": outcome.get("error_summary"),
        }
        if existing and existing.get("id"):
            fields["id"] = existing["id"]
            fields.setdefault("started_at", existing.get("started_at"))
            fields.setdefault("model_provider", existing.get("model_provider"))
            fields.setdefault("model", existing.get("model"))
            fields.setdefault("prompt_id", existing.get("prompt_id"))
            fields.setdefault("trace_dir", existing.get("trace_dir"))
        upsert_trace(**fields)
    except Exception:
        pass


def reconcile_in_progress(log_path: str | Path = DEFAULT_GATEWAY_LOG) -> list[dict[str, Any]]:
    """Move stuck in-progress items to completed/failed when gateway has final output."""
    import queue as queue_mod  # local queue.py when run from BASE_DIR/sys.path
    from events import log_event

    outcomes = scan_gateway_log(log_path)
    if not outcomes:
        return []

    q = queue_mod.load_queue()
    reconciled: list[dict[str, Any]] = []
    for item in list(q.get("in_progress", [])):
        dispatch_id = _item_dispatch_id(item)
        if not dispatch_id:
            continue
        outcome = outcomes.get(dispatch_id)
        if not outcome:
            continue

        _write_trace_final(item, outcome)
        _update_agent_trace(item, outcome)
        if outcome["status"] == "completed":
            changed = queue_mod.complete(item["id"], outcome.get("pr_number"))
        elif outcome["status"] == "non_contract":
            linked_prs = []
            try:
                linked_prs = queue_mod.check_linked_prs(item.get("repo"), item.get("issue_number")) or []
            except Exception:
                linked_prs = []
            linked_pr = linked_prs[0] if linked_prs else None
            if linked_pr and linked_pr.get("pr_number"):
                outcome["status"] = "completed"
                outcome["pr_number"] = linked_pr.get("pr_number")
                changed = queue_mod.complete(item["id"], outcome.get("pr_number"))
            else:
                changed = queue_mod.fail(item["id"], outcome.get("error_summary") or "Worker finished without contract output")
        else:
            changed = queue_mod.fail(item["id"], outcome.get("error_summary") or "FAILED: gateway worker failed")
        if changed:
            record = {
                "item_id": item["id"],
                "repo": item.get("repo"),
                "issue_number": item.get("issue_number"),
                "dispatch_id": dispatch_id,
                "status": outcome["status"],
                "pr_number": outcome.get("pr_number"),
                "error_summary": outcome.get("error_summary"),
            }
            log_event(
                "issue.reconciled",
                item_id=item.get("id"),
                repo=item.get("repo"),
                issue_number=item.get("issue_number"),
                title=item.get("title"),
                details=record,
            )
            reconciled.append(record)
    return reconciled


if __name__ == "__main__":
    results = reconcile_in_progress()
    print(json.dumps({"reconciled": results}, indent=2, default=str))
