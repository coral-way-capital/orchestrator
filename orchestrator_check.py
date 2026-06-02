#!/usr/bin/env python3
"""
CWC Issue Orchestrator — pre-processing script for the cron job.
Reads the queue, determines what to dispatch, and outputs context for the agent.

Output is injected into the cron prompt as context.
"""

import json
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from queue import load_queue

MAX_CONCURRENT = 2

# Map GitHub repo to local path
REPO_MAP = {
    "coral-way-capital/audit-agent": "/home/deploy/apps/audit-agent",
    "coral-way-capital/website": "/var/www/coralwaycapital",
    "coral-way-capital/rsm-monitor": "/home/deploy/apps/eckhart",
    "coral-way-capital/eckhart": "/home/deploy/apps/eckhart",
    "coral-way-capital/zenna-crm": "/home/deploy/apps/zenna-crm",
    "coral-way-capital/inmuebles": "/home/deploy/apps/inmuebles",
    "coral-way-capital/infrastructure": "/home/deploy/apps/infrastructure",
    "coral-way-capital/tasks-cli": "/home/deploy/apps/tasks-cli",
    "coral-way-capital/agent-configs": "/home/deploy/apps/agent-configs",
    "coral-way-capital/sre": "/home/deploy/apps/sre",
}


def main():
    queue = load_queue()
    pending = len(queue["pending"])
    in_progress = len(queue["in_progress"])
    completed = len(queue["completed"])
    failed = len(queue["failed"])
    slots = MAX_CONCURRENT - in_progress

    print(f"=== CWC Issue Queue Status ===")
    print(f"Pending: {pending} | In Progress: {in_progress} | Completed: {completed} | Failed: {failed}")
    print(f"Available slots: {slots}")

    if slots <= 0:
        print("\nNO ACTION: All slots full. Workers still running.")
        # Show what's running
        for item in queue["in_progress"]:
            print(f"  🔄 {item['id']}: {item['title'][:60]} (since {item.get('started_at', 'unknown')})")
        return

    if pending == 0:
        print("\nNO ACTION: Queue empty.")
        return

    # Claim items
    to_dispatch = queue["pending"][:slots]

    # Check for same-repo conflicts — only 1 worker per repo per cycle
    repos_in_progress = {item["repo"] for item in queue["in_progress"]}
    dispatch_repos = set()
    filtered = []
    for item in to_dispatch:
        if item["repo"] in repos_in_progress:
            print(f"SKIP: {item['id']} — repo already has a worker in progress")
            continue
        if item["repo"] in dispatch_repos:
            print(f"SKIP: {item['id']} — another item in same repo already dispatching this cycle")
            continue
        dispatch_repos.add(item["repo"])
        filtered.append(item)

    if not filtered:
        print("\nNO ACTION: All pending items conflict with in-progress repos.")
        return

    print(f"\n=== Dispatching {len(filtered)} workers ===")
    for item in filtered:
        local_path = REPO_MAP.get(item["repo"], f"/home/deploy/apps/{item['repo'].split('/')[-1]}")
        print(f"\n--- ISSUE: {item['id']} ---")
        print(f"Title: {item['title']}")
        print(f"Repo: {item['repo']} → {local_path}")
        print(f"Labels: {', '.join(item.get('labels', []))}")
        print(f"URL: {item.get('html_url', 'N/A')}")
        print(f"Body preview: {(item.get('body', '') or '')[:200]}...")
        print(f"Local path exists: {os.path.exists(local_path)}")


if __name__ == "__main__":
    main()
