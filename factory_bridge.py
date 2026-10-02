#!/usr/bin/env python3
"""GitHub admission to native Hermes Kanban. No agent, queue, or worker loop here."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from eligibility import allowed_assignees, is_assigned_to_allowed
from portfolio import outcome_contract_for_repo

BOARD = "factory"
ROOT = Path.home() / ".hermes"
SKIP_LABELS = {"question", "discussion", "wontfix", "duplicate", "invalid", "docs", "epic-child"}
REPO = re.compile(r"coral-way-capital/[A-Za-z0-9_.-]+")


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120, **kwargs)
    if result.returncode:
        # CLI errors may include credentials or private response bodies.
        raise RuntimeError(f"{Path(args[0]).name} failed (exit {result.returncode})")
    return result.stdout


def gh(endpoint, *, pages=False):
    args = ["gh", "api", endpoint]
    if pages:
        args += ["--paginate", "--slurp"]
    data = json.loads(command(args))
    if isinstance(data, dict) and data.get("errors"):
        raise RuntimeError("Incomplete GitHub response")
    return data


def eligibility_reason(repo, issue):
    if not REPO.fullmatch(repo) or not isinstance(issue.get("number"), int) or issue["number"] < 1:
        raise ValueError("Invalid CWC issue locator")
    # Preserve Maya's existing exclusion when the environment has no override.
    disabled = set(os.environ.get("CWC_DISABLED_ORCHESTRATOR_REPOS", "coral-way-capital/visit-merida-chatbot").split(","))
    if repo in {x.strip() for x in disabled}:
        return "repository disabled"
    if issue.get("state") != "open" or "pull_request" in issue:
        return "not an open issue"
    if not is_assigned_to_allowed(issue.get("assignees")):
        return "not assigned to an allowed user"
    labels = {x["name"].lower() if isinstance(x, dict) else str(x).lower() for x in issue.get("labels", [])}
    if labels & SKIP_LABELS or re.match(r"^\[Parent #\d+\]", issue.get("title", "")):
        return "excluded label or decomposition child"
    return None


def linked_pr(repo, number):
    pages = gh(f"repos/{repo}/issues/{number}/timeline?per_page=100", pages=True)
    for page in pages:
        for event in page:
            source = (event.get("source") or {}).get("issue") or {}
            if source.get("pull_request") and source.get("state") == "open":
                return True
    return False


def workspace(repo, number):
    """Dedicated checkout anchor; native Kanban materializes each task's worktree."""
    anchor = ROOT / "factory" / "repos" / repo
    if not anchor.exists():
        anchor.parent.mkdir(parents=True, exist_ok=True)
        command(["git", "clone", "--no-checkout", f"https://github.com/{repo}.git", str(anchor)])
    origin = command(["git", "-C", str(anchor), "remote", "get-url", "origin"]).strip()
    if origin != f"https://github.com/{repo}.git":
        raise RuntimeError("Factory checkout origin mismatch")
    default = gh(f"repos/{repo}")["default_branch"]
    command(["git", "-C", str(anchor), "fetch", "origin", f"refs/heads/{default}"])
    branch = f"factory/issue-{number}-{uuid.uuid4().hex[:12]}"
    command(["git", "-C", str(anchor), "branch", branch, "FETCH_HEAD"])
    return str(anchor), branch


def intake(kb, conn, repo, issue, *, legacy_status=None):
    reason = eligibility_reason(repo, issue)
    key = f"github:{repo}#{issue['number']}"
    tasks = kb.list_tasks(conn, include_archived=True)
    existing = next((t for t in tasks if t.idempotency_key == key), None)
    if existing:
        if reason and existing.status in {"ready", "todo"}:
            kb.block_task(conn, existing.id, reason=f"GitHub intake: {reason}", kind="needs_input")
        return None
    if reason and legacy_status is None:
        return None
    if legacy_status is None and linked_pr(repo, issue["number"]):
        return None
    contract = outcome_contract_for_repo(repo)  # Fail closed on unavailable client policy.
    historical = legacy_status is not None
    anchor, branch = (None, None) if historical else workspace(repo, issue["number"])
    # Native dependencies serialize each repository through implementation AND review.
    # ponytail: one lane per repository; widen only with an explicit concurrency decision.
    parents = [] if historical else [t.id for t in tasks if t.created_by == "github-intake" and t.tenant == repo and t.status not in {"done", "archived"}]
    source = {"repository": repo, "issue_number": issue["number"],
              "issue_url": f"https://github.com/{repo}/issues/{issue['number']}",
              "outcome_contract": contract}
    body = "GitHub is the specification authority. Fetch the issue and repository AGENTS.md before work.\n"
    body += "Preserve this issue's durable card across retries; never archive it to bypass deduplication.\n"
    body += "Recheck assignment, state, dependencies and linked PRs before editing and before publishing.\n"
    body += "Implement -> factory-reviewer using kanban_request_review with PR URL, exact head SHA, tests and remaining risks.\n"
    body += "A reviewed PR is engineering evidence; client acceptance requires the outcome contract's separate evidence.\n"
    body += json.dumps(source, indent=2) + "\n"
    if historical:
        body += f"Legacy Mission Control state: {legacy_status}. History remains in ~/.hermes/issue-queue.\n"
    task_id = kb.create_task(
        conn, board=BOARD, title=f"{repo.split('/')[-1]} #{issue['number']}: {issue.get('title', '')}",
        body=body, tenant=repo, created_by="factory-migration" if historical else "github-intake",
        assignee="factory-implementer", workspace_kind="scratch" if historical else "worktree",
        workspace_path=anchor, branch_name=branch, parents=parents, idempotency_key=key,
        initial_status="blocked" if historical else "running",
        max_runtime_seconds=5400, max_retries=1,
        completion_contract="local-only" if historical else repo,
    )
    if historical:
        kb.add_comment(conn, task_id, "factory-migration",
                       f"Imported as an inert history reference. Legacy state: {legacy_status}; {reason or 'explicit retry required'}. "
                       "Use a fresh GitHub eligibility check and a managed worktree before any retry.")
        if legacy_status == "completed":
            kb.complete_task(conn, task_id, result="Imported historical completion; no new run or client acceptance asserted.")
    return task_id


@contextlib.contextmanager
def board():
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    conn = connect(board=BOARD)
    # Serialize admission's dedupe + per-repo graph calculation; execution is native.
    lock = kb.kanban_home() / "kanban" / "factory-intake.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock.open("a") as file:
            fcntl.flock(file, fcntl.LOCK_EX)
            yield kb, conn
    finally:
        conn.close()


def current_issue(repo, number):
    if not REPO.fullmatch(repo) or not isinstance(number, int) or number < 1:
        raise ValueError("Invalid issue locator")
    return gh(f"repos/{repo}/issues/{number}")


def run(*, payload=None, legacy=None):
    with board() as (kb, conn):
        if legacy is not None:
            if legacy.get("in_progress"):
                raise RuntimeError("Drain legacy workers before import")
            for status in ("completed", "failed", "pending"):
                for item in legacy.get(status, []):
                    issue = {**item, "number": item["issue_number"], "state": "open"}
                    intake(kb, conn, item["repo"], issue, legacy_status=status)
            return
        if payload is not None:
            if "issue" not in payload:
                return
            repo = (payload.get("repository") or {}).get("full_name", "")
            intake(kb, conn, repo, current_issue(repo, payload["issue"].get("number")))
            return
        seen = set()
        for assignee in sorted(allowed_assignees()):
            from urllib.parse import quote
            query = quote(f"org:coral-way-capital is:issue is:open assignee:{assignee}")
            pages = gh(f"search/issues?q={query}&per_page=100", pages=True)
            if any(p.get("incomplete_results") or p["total_count"] > 1000 for p in pages):
                raise RuntimeError("GitHub issue search is incomplete; intake stopped")
            for page in pages:
                for issue in page["items"]:
                    repo = issue["repository_url"].split("/repos/", 1)[1]
                    seen.add(f"github:{repo}#{issue['number']}")
                    # Fetch current state; search results and webhook deliveries can lag.
                    intake(kb, conn, repo, current_issue(repo, issue["number"]))
        for task in kb.list_tasks(conn):
            if task.created_by == "github-intake" and task.status in {"ready", "todo"} and task.idempotency_key not in seen:
                repo, number = task.idempotency_key[7:].rsplit("#", 1)
                intake(kb, conn, repo, current_issue(repo, int(number)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--webhook", action="store_true")
    mode.add_argument("--sync", action="store_true")
    mode.add_argument("--import-legacy", type=Path)
    args = parser.parse_args()
    try:
        run(payload=json.load(sys.stdin) if args.webhook else None,
            legacy=json.loads(args.import_legacy.read_text()) if args.import_legacy else None)
    except Exception as exc:
        print(f"Factory intake failed ({type(exc).__name__}); inspect GitHub access and Kanban diagnostics.", file=sys.stderr)
        return 1
    return 0  # Empty stdout prevents an LLM invocation in native webhook/cron scripts.


if __name__ == "__main__":
    raise SystemExit(main())
