#!/usr/bin/env python3
"""Mission Control eligibility engine.

Keeps dashboard visibility separate from dispatch eligibility. GitHub issue data
is the mutable projection; queue state only describes orchestration lifecycle.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
QUEUE_FILE = BASE_DIR / "queue.json"
WORKER_POOLS_FILE = BASE_DIR / "worker_pools.json"


def assignee_logins(assignees: list[Any] | None) -> list[str]:
    logins: list[str] = []
    for assignee in assignees or []:
        if isinstance(assignee, str):
            login = assignee
        elif isinstance(assignee, dict):
            login = assignee.get("login", "")
        else:
            login = ""
        if login:
            logins.append(str(login).lower())
    return logins


@lru_cache(maxsize=8)
def allowed_assignees() -> frozenset[str]:
    configured = os.environ.get("CWC_ISSUE_ASSIGNEES", "").strip()
    if configured:
        return frozenset(x.strip().lower() for x in configured.split(",") if x.strip())
    try:
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return frozenset([result.stdout.strip().lower()])
    except Exception:
        pass
    return frozenset(["ivanacostarubio"])


def visible_unassigned_repos() -> set[str]:
    configured = os.environ.get("CWC_VISIBLE_UNASSIGNED_REPOS", "").strip()
    if configured:
        return {x.strip() for x in configured.split(",") if x.strip()}
    return {"coral-way-capital/visit-merida-chatbot"}


def is_assigned_to_allowed(assignees: list[Any] | None) -> bool:
    return bool(set(assignee_logins(assignees)) & allowed_assignees())


def load_queue() -> dict[str, list[dict[str, Any]]]:
    if QUEUE_FILE.exists():
        return json.loads(QUEUE_FILE.read_text())
    return {"pending": [], "in_progress": [], "completed": [], "failed": []}


def active_workers() -> list[dict[str, Any]]:
    if not WORKER_POOLS_FILE.exists():
        return []
    try:
        data = json.loads(WORKER_POOLS_FILE.read_text())
        return [w for w in data.get("workers", []) if w.get("state", "active") == "active"]
    except Exception:
        return []


def issue_visible(item: dict[str, Any]) -> bool:
    if item.get("state") == "closed" or item.get("closed_via"):
        return False
    if is_assigned_to_allowed(item.get("assignees", [])):
        return True
    return item.get("repo") in visible_unassigned_repos()


def evaluate_item(
    item: dict[str, Any],
    queue_status: str = "pending",
    active_repo_locks: set[str] | None = None,
    linked_prs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return explicit visibility/eligibility status + reasons for one issue."""
    active_repo_locks = active_repo_locks or set()
    linked_prs = linked_prs or []
    assignees = assignee_logins(item.get("assignees", []))
    allowed = sorted(allowed_assignees())
    visible = issue_visible(item)
    reasons: list[dict[str, Any]] = []

    if not visible:
        reasons.append({"code": "not_visible", "message": "Issue is neither assigned to allowed users nor opted into unassigned visibility"})
    if not assignees:
        reasons.append({"code": "unassigned", "message": "Issue has no GitHub assignee"})
    elif not (set(assignees) & set(allowed)):
        reasons.append({"code": "outside_assignee", "message": "Issue is not assigned to an allowed orchestrator user", "assignees": assignees, "allowed": allowed})
    if queue_status == "in_progress":
        reasons.append({"code": "already_in_progress", "message": "A worker is already active for this issue"})
    elif queue_status == "completed":
        reasons.append({"code": "completed", "message": "Issue queue item is already completed"})
    elif queue_status == "failed":
        reasons.append({"code": "failed", "message": "Issue queue item is failed and needs retry"})
    if queue_status == "pending" and item.get("repo") in active_repo_locks:
        reasons.append({"code": "repo_locked", "message": "Another worker is active for this repo"})
    if linked_prs:
        reasons.append({"code": "linked_pr", "message": "Issue already has an open linked PR", "prs": linked_prs})

    eligible = bool(visible and queue_status == "pending" and is_assigned_to_allowed(item.get("assignees", [])) and not linked_prs and item.get("repo") not in active_repo_locks)
    state = "eligible" if eligible else ("visible_blocked" if visible else "hidden")
    return {
        "item_id": item.get("id"),
        "repo": item.get("repo"),
        "issue_number": item.get("issue_number"),
        "title": item.get("title"),
        "queue_status": queue_status,
        "visible": bool(visible),
        "eligible": bool(eligible),
        "state": state,
        "assignees": assignees,
        "allowed_assignees": allowed,
        "blocked_reasons": reasons,
    }


def queue_with_status(queue: dict[str, list[dict[str, Any]]] | None = None) -> list[tuple[str, dict[str, Any]]]:
    queue = queue or load_queue()
    rows: list[tuple[str, dict[str, Any]]] = []
    for status in ("pending", "in_progress", "failed", "completed"):
        for item in queue.get(status, []):
            rows.append((status, item))
    return rows


def repo_diagnostics(repo: str | None = None) -> dict[str, Any]:
    """Fast local diagnostics; uses queue + worker-pool state, no per-issue GraphQL."""
    queue = load_queue()
    workers = active_workers()
    active_repo_locks: set[str] = {str(w.get("repo")) for w in workers if w.get("repo")}
    issue_rows = []
    by_repo: dict[str, dict[str, Any]] = {}

    for status, item in queue_with_status(queue):
        if repo and item.get("repo") != repo:
            continue
        diag = evaluate_item(item, queue_status=status, active_repo_locks=active_repo_locks)
        issue_rows.append(diag)
        r = diag["repo"] or "unknown"
        bucket = by_repo.setdefault(r, {
            "repo": r,
            "visible": 0,
            "eligible": 0,
            "pending": 0,
            "in_progress": 0,
            "failed": 0,
            "completed": 0,
            "blocked": 0,
            "block_reasons": Counter(),
            "repo_lock": None,
        })
        bucket[status] = bucket.get(status, 0) + 1
        if diag["visible"]:
            bucket["visible"] += 1
        if diag["eligible"]:
            bucket["eligible"] += 1
        if diag["visible"] and not diag["eligible"]:
            bucket["blocked"] += 1
            for reason in diag["blocked_reasons"]:
                bucket["block_reasons"][reason["code"]] += 1

    for w in workers:
        r = w.get("repo")
        if r in by_repo:
            by_repo[r]["repo_lock"] = {"item_id": w.get("item_id"), "worker_id": w.get("id"), "started_at": w.get("started_at")}

    summaries = []
    for bucket in by_repo.values():
        bucket["block_reasons"] = dict(bucket["block_reasons"])
        summaries.append(bucket)
    summaries.sort(key=lambda x: x["repo"])

    alerts = []
    for s in summaries:
        if s["visible"] > 0 and s["eligible"] == 0 and not s.get("repo_lock"):
            alerts.append({"repo": s["repo"], "severity": "red", "message": f"{s['visible']} issues visible, 0 eligible", "block_reasons": s["block_reasons"]})
        elif s["visible"] > 0 and s["eligible"] == 0 and s.get("repo_lock"):
            alerts.append({"repo": s["repo"], "severity": "amber", "message": f"{s['visible']} issues visible, repo locked by {s['repo_lock']['item_id']}", "block_reasons": s["block_reasons"]})

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "allowed_assignees": sorted(allowed_assignees()),
        "visible_unassigned_repos": sorted(visible_unassigned_repos()),
        "repos": summaries,
        "issues": issue_rows,
        "alerts": alerts,
    }
