#!/usr/bin/env python3
"""
CWC Issue Queue — manages the pending/in-progress/completed queue for GitHub issues.
Used by the webhook (enqueue) and orchestrator (dequeue + dispatch).

Queue file: ~/.hermes/issue-queue/queue.json
{
  "pending": [...],
  "in_progress": [...],
  "completed": [...],
  "failed": [...]
}

Each item:
{
  "id": "coral-way-capital/audit-agent#42",
  "repo": "coral-way-capital/audit-agent",
  "issue_number": 42,
  "title": "...",
  "body": "...",
  "author": "...",
  "labels": ["bug", "p0"],
  "html_url": "https://...",
  "enqueued_at": "2026-05-17T17:30:00Z",
  "started_at": null,
  "completed_at": null,
  "pr_number": null,
  "error": null
}
"""

import json
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

QUEUE_FILE = Path.home() / ".hermes" / "issue-queue" / "queue.json"
MAX_COMPLETED = 100  # keep last N completed items

# Event logger
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from events import log_event
except ImportError:
    def log_event(*a, **kw):
        pass  # Graceful fallback if events.py missing


def load_queue():
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE) as f:
            return json.load(f)
    return {"pending": [], "in_progress": [], "completed": [], "failed": []}


def save_queue(queue):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Trim completed list
    if len(queue["completed"]) > MAX_COMPLETED:
        queue["completed"] = queue["completed"][-MAX_COMPLETED:]
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def check_linked_prs(repo, issue_number):
    """Check if an issue already has open linked PRs via GitHub GraphQL API."""
    import subprocess
    owner, name = repo.split("/")
    query = json.dumps({
        "query": """query($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                issue(number: $number) {
                    timelineItems(first: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
                        nodes {
                            ... on CrossReferencedEvent {
                                source {
                                    ... on PullRequest { number state title }
                                }
                            }
                        }
                    }
                }
            }
        }""",
        "variables": {"owner": owner, "name": name, "number": issue_number}
    })

    try:
        result = subprocess.run(
            ["gh", "api", "graphql", "--input", "-"],
            input=query, capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            nodes = (data.get("data", {})
                     .get("repository", {})
                     .get("issue", {})
                     .get("timelineItems", {})
                     .get("nodes", []))
            open_prs = [
                {"pr_number": n["source"]["number"], "pr_title": n["source"]["title"]}
                for n in nodes
                if n.get("source", {}).get("state") == "OPEN"
            ]
            return open_prs
    except Exception as e:
        print(f"WARN: Could not check linked PRs for {repo}#{issue_number}: {e}")
    return []


def enqueue(repo, issue_number, title, body, author, labels, html_url):
    """Add an issue to the pending queue. Skips duplicates and unwanted labels."""
    queue = load_queue()
    item_id = f"{repo}#{issue_number}"

    # Skip if already in pending or in_progress
    all_ids = (
        [x["id"] for x in queue["pending"]]
        + [x["id"] for x in queue["in_progress"]]
    )
    if item_id in all_ids:
        print(f"SKIP: {item_id} already in queue")
        return False

    # Skip unwanted labels
    skip_labels = {"question", "discussion", "wontfix", "duplicate", "invalid", "docs"}
    if any(l.lower() in skip_labels for l in labels):
        print(f"SKIP: {item_id} has skip label")
        return False

    # Skip if issue already has open linked PRs
    linked_prs = check_linked_prs(repo, issue_number)
    if linked_prs:
        pr_nums = ", ".join(f"#{p['pr_number']}" for p in linked_prs)
        print(f"SKIP: {item_id} already has open linked PR(s): {pr_nums}")
        return False

    item = {
        "id": item_id,
        "repo": repo,
        "issue_number": issue_number,
        "title": title,
        "body": body or "",
        "author": author,
        "labels": labels,
        "html_url": html_url,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
        "started_at": None,
        "completed_at": None,
        "pr_number": None,
        "error": None,
    }

    queue["pending"].append(item)
    save_queue(queue)
    log_event("issue.enqueued", item_id=item_id, repo=repo,
              issue_number=issue_number, title=title,
              details={"author": author, "labels": labels})
    print(f"ENQUEUED: {item_id} — {title}")
    return True


def next_pending(n=1):
    """Get up to N pending items and move them to in_progress."""
    queue = load_queue()
    items = queue["pending"][:n]
    for item in items:
        item["started_at"] = datetime.now(timezone.utc).isoformat()
        queue["pending"].remove(item)
        queue["in_progress"].append(item)
        log_event("issue.claimed", item_id=item["id"], repo=item.get("repo"),
                  issue_number=item.get("issue_number"), title=item.get("title"))
    save_queue(queue)
    return items


def complete(item_id, pr_number=None):
    """Move an item from in_progress to completed."""
    queue = load_queue()
    for item in queue["in_progress"]:
        if item["id"] == item_id:
            item["completed_at"] = datetime.now(timezone.utc).isoformat()
            item["pr_number"] = pr_number
            queue["in_progress"].remove(item)
            queue["completed"].append(item)
            save_queue(queue)
            log_event("issue.completed", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"pr_number": pr_number})
            print(f"COMPLETED: {item_id} PR#{pr_number}")
            return True
    print(f"NOT FOUND in in_progress: {item_id}")
    return False


def fail(item_id, error):
    """Move an item from in_progress to failed."""
    queue = load_queue()
    for item in queue["in_progress"]:
        if item["id"] == item_id:
            item["completed_at"] = datetime.now(timezone.utc).isoformat()
            item["error"] = str(error)[:500]
            queue["in_progress"].remove(item)
            queue["failed"].append(item)
            save_queue(queue)
            log_event("issue.failed", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"error": str(error)[:200]})
            print(f"FAILED: {item_id} — {error}")
            return True
    print(f"NOT FOUND in in_progress: {item_id}")
    return False


def retry(item_id):
    """Move a failed item back to pending."""
    queue = load_queue()
    for item in queue["failed"]:
        if item["id"] == item_id:
            item["started_at"] = None
            item["completed_at"] = None
            item["pr_number"] = None
            item["error"] = None
            queue["failed"].remove(item)
            queue["pending"].append(item)
            save_queue(queue)
            log_event("issue.retried", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"))
            print(f"RETRY: {item_id}")
            return True
    print(f"NOT FOUND in failed: {item_id}")
    return False


def status():
    """Print queue status."""
    queue = load_queue()
    p = len(queue["pending"])
    ip = len(queue["in_progress"])
    c = len(queue["completed"])
    f = len(queue["failed"])
    print(f"Queue: {p} pending | {ip} in_progress | {c} completed | {f} failed")

    if queue["in_progress"]:
        print("\n--- In Progress ---")
        for item in queue["in_progress"]:
            print(f"  {item['id']}: {item['title'][:60]}")

    if queue["pending"]:
        print(f"\n--- Pending ({p}) ---")
        for item in queue["pending"][:10]:
            print(f"  {item['id']}: {item['title'][:60]}")
        if p > 10:
            print(f"  ... and {p - 10} more")

    if queue["failed"]:
        print(f"\n--- Failed ({f}) ---")
        for item in queue["failed"][:5]:
            print(f"  {item['id']}: {item['error'][:80] if item['error'] else 'unknown'}")

    return queue


def reset(item_id):
    """Remove item from any queue (for manual cleanup)."""
    queue = load_queue()
    for lst_name in ["pending", "in_progress", "completed", "failed"]:
        for item in queue[lst_name]:
            if item["id"] == item_id:
                queue[lst_name].remove(item)
                save_queue(queue)
                log_event("issue.reset", item_id=item_id, repo=item.get("repo"),
                          issue_number=item.get("issue_number"), title=item.get("title"),
                          details={"from_list": lst_name})
                print(f"REMOVED: {item_id} from {lst_name}")
                return True
    print(f"NOT FOUND: {item_id}")
    return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: queue.py <status|enqueue|next|complete|fail|retry|reset> ...")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        status()
    elif cmd == "enqueue":
        # queue.py enqueue <repo> <issue_number> <title> <author> <labels_json> <html_url> [body]
        repo = sys.argv[2]
        num = int(sys.argv[3])
        title = sys.argv[4]
        author = sys.argv[5]
        labels = json.loads(sys.argv[6])
        url = sys.argv[7]
        body = sys.argv[8] if len(sys.argv) > 8 else ""
        enqueue(repo, num, title, body, author, labels, url)
    elif cmd == "next":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        items = next_pending(n)
        if items:
            print(json.dumps(items, indent=2))
        else:
            print("NO PENDING ITEMS")
    elif cmd == "complete":
        item_id = sys.argv[2]
        pr = int(sys.argv[3]) if len(sys.argv) > 3 else None
        complete(item_id, pr)
    elif cmd == "fail":
        item_id = sys.argv[2]
        error = sys.argv[3] if len(sys.argv) > 3 else "unknown error"
        fail(item_id, error)
    elif cmd == "retry":
        item_id = sys.argv[2]
        retry(item_id)
    elif cmd == "reset":
        item_id = sys.argv[2]
        reset(item_id)
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
