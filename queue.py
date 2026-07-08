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
SYNC_STATE_FILE = QUEUE_FILE.parent / "sync-state.json"
MAX_COMPLETED = 100  # keep last N completed items

# Event logger
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from events import log_event
except ImportError:
    def log_event(*a, **kw):
        pass  # Graceful fallback if events.py missing

try:
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from worker_pools import WorkerPoolsManager
    _POOLS = WorkerPoolsManager(os.path.join(Path.home(), ".hermes", "issue-queue", "worker_pools.json"))

    def _pool_cleanup(item_id):
        try:
            _POOLS.remove_worker_by_item(item_id)
        except Exception:
            pass
except ImportError:
    def _pool_cleanup(item_id):
        pass


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
    # Compatibility ledger: queue.json remains the dashboard source of truth,
    # but every mutation is mirrored into SQLite for transactional dispatch
    # state, priorities, attempts, and telemetry.
    try:
        import issue_queue_db
        issue_queue_db.sync_from_queue(queue)
    except Exception:
        pass


def load_sync_state():
    """Load per-repo last-sync timestamps. Returns {repo: iso_timestamp}."""
    if SYNC_STATE_FILE.exists():
        try:
            with open(SYNC_STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def discover_org_repos(org="coral-way-capital"):
    """Return active org repos visible to gh.

    Mission Control must not rely only on repos already present in queue.json:
    brand-new repos have no queue items yet, so they would never enter the sync
    universe unless their GitHub webhook fired successfully. Discovery is still
    filtered later by the assignee guard; this only defines which repos to scan.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "gh", "repo", "list", org,
                "--limit", "200",
                "--json", "nameWithOwner,isArchived",
                "--jq", ".[] | select(.isArchived == false) | .nameWithOwner",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return []
        return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
    except Exception:
        return []


def save_sync_state(state):
    """Persist sync state (per-repo timestamps)."""
    SYNC_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SYNC_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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


def allowed_assignees():
    """Return GitHub usernames this orchestrator is allowed to work for.

    Defaults to the authenticated gh user. Override with CWC_ISSUE_ASSIGNEES
    as a comma-separated list, e.g. "ivanacostarubio,another-user".
    """
    configured = os.environ.get("CWC_ISSUE_ASSIGNEES", "").strip()
    if configured:
        return {x.strip().lower() for x in configured.split(",") if x.strip()}

    try:
        import subprocess
        result = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {result.stdout.strip().lower()}
    except Exception:
        pass

    # Safe fallback: Ivan is the CWC account currently authenticated on maya.
    return {"ivanacostarubio"}


def _assignee_logins(assignees):
    logins = []
    for assignee in assignees or []:
        if isinstance(assignee, str):
            login = assignee
        elif isinstance(assignee, dict):
            login = assignee.get("login", "")
        else:
            login = ""
        if login:
            logins.append(login.lower())
    return logins


def is_issue_assigned_to_allowed(assignees):
    """True iff the issue has at least one allowed assignee.

    Unassigned issues are intentionally not eligible for the orchestrator.
    """
    return bool(set(_assignee_logins(assignees)) & allowed_assignees())


def visible_unassigned_repos():
    """Repos where unassigned issues should appear on the board.

    Dispatch remains assignee-gated; this is dashboard visibility only.
    Override with CWC_VISIBLE_UNASSIGNED_REPOS=owner/repo,owner/repo.
    """
    configured = os.environ.get("CWC_VISIBLE_UNASSIGNED_REPOS", "").strip()
    if configured:
        return {x.strip() for x in configured.split(",") if x.strip()}
    return {"coral-way-capital/visit-merida-chatbot"}


def enqueue(repo, issue_number, title, body, author, labels, html_url, assignees=None, require_allowed_assignee=True):
    """Add an issue to the pending queue. Skips duplicates, unwanted labels, and non-CWC-assigned work."""
    queue = load_queue()
    item_id = f"{repo}#{issue_number}"

    # If already queued, refresh mutable GitHub metadata (especially assignees).
    # Assignment can happen after initial visibility sync; stale [] assignees make
    # an item visible but non-dispatchable forever unless we refresh here.
    for bucket in ("pending", "in_progress"):
        for existing in queue.get(bucket, []):
            if existing.get("id") == item_id:
                old_assignees = list(existing.get("assignees", []))
                old_eligible = is_issue_assigned_to_allowed(old_assignees)
                changed = False
                updates = {
                    "title": title,
                    "body": body or "",
                    "author": author,
                    "labels": labels,
                    "assignees": _assignee_logins(assignees),
                    "html_url": html_url,
                }
                for key, value in updates.items():
                    if existing.get(key) != value:
                        existing[key] = value
                        changed = True
                if changed:
                    save_queue(queue)
                    log_event("issue.metadata_refreshed", item_id=item_id, repo=repo,
                              issue_number=issue_number, title=title,
                              details={"bucket": bucket, "old_assignees": old_assignees, "assignees": _assignee_logins(assignees), "labels": labels})
                    new_eligible = is_issue_assigned_to_allowed(existing.get("assignees", []))
                    if old_eligible != new_eligible:
                        log_event("issue.eligibility_changed", item_id=item_id, repo=repo,
                                  issue_number=issue_number, title=title,
                                  details={"from": old_eligible, "to": new_eligible, "assignees": existing.get("assignees", []), "allowed": sorted(allowed_assignees())})
                    print(f"REFRESHED: {item_id} metadata in {bucket}")
                else:
                    print(f"SKIP: {item_id} already in queue")
                return False

    if require_allowed_assignee and not is_issue_assigned_to_allowed(assignees):
        allowed = ", ".join(sorted(allowed_assignees()))
        actual = ", ".join(_assignee_logins(assignees)) or "unassigned"
        print(f"SKIP: {item_id} assignees={actual}; allowed={allowed}")
        log_event("issue.skipped_unassigned", item_id=item_id, repo=repo,
                  issue_number=issue_number, title=title,
                  details={"assignees": _assignee_logins(assignees), "allowed": sorted(allowed_assignees())})
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
        "assignees": _assignee_logins(assignees),
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
              details={"author": author, "labels": labels, "assignees": _assignee_logins(assignees)})
    log_event("issue.visible", item_id=item_id, repo=repo,
              issue_number=issue_number, title=title,
              details={"source": "enqueue", "assignees": _assignee_logins(assignees), "visible_unassigned": repo in visible_unassigned_repos()})
    log_event("issue.eligibility_changed", item_id=item_id, repo=repo,
              issue_number=issue_number, title=title,
              details={"from": None, "to": is_issue_assigned_to_allowed(item.get("assignees", [])), "assignees": item.get("assignees", []), "allowed": sorted(allowed_assignees())})
    print(f"ENQUEUED: {item_id} — {title}")
    return True


def _pool_reap():
    try:
        from worker_pools import WorkerPoolsManager
        mgr = WorkerPoolsManager(os.path.join(Path.home(), ".hermes", "issue-queue", "worker_pools.json"))
        mgr.reap_stale()
    except Exception:
        pass


def _pool_register(item_id, repo):
    try:
        from worker_pools import WorkerPoolsManager
        mgr = WorkerPoolsManager(os.path.join(Path.home(), ".hermes", "issue-queue", "worker_pools.json"))
        mgr.register_worker(item_id, repo)
    except Exception:
        pass


def next_pending(n=1):
    """Get up to N pending items and move them to in_progress."""
    queue = load_queue()
    _pool_reap()
    claimed = []
    # Priority router: not FIFO. Work client/prod/p0/bug issues first while
    # preserving enqueue order inside equal priority bands.
    try:
        import issue_queue_db
        pending_items = sorted(list(queue["pending"]), key=lambda i: (-issue_queue_db.compute_priority(i), i.get("enqueued_at") or ""))
    except Exception:
        pending_items = list(queue["pending"])

    for item in pending_items:
        if not is_issue_assigned_to_allowed(item.get("assignees", [])):
            log_event("issue.skipped_unassigned", item_id=item.get("id"), repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"source": "claim_guard", "assignees": item.get("assignees", []), "allowed": sorted(allowed_assignees())})
            continue
        if item["repo"] in {w.get("repo") for w in __import__("worker_pools", fromlist=["WorkerPoolsManager"]).WorkerPoolsManager(os.path.join(Path.home(), ".hermes", "issue-queue", "worker_pools.json")).workers() if w.get("state") == "active"}:
            continue
        item["started_at"] = datetime.now(timezone.utc).isoformat()
        queue["pending"].remove(item)
        queue["in_progress"].append(item)
        claimed.append(item)
        log_event("issue.claimed", item_id=item["id"], repo=item.get("repo"),
                  issue_number=item.get("issue_number"), title=item.get("title"))
        _pool_register(item["id"], item.get("repo"))
        if len(claimed) >= n:
            break
    save_queue(queue)
    return claimed


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
            _pool_cleanup(item_id)
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
            _pool_cleanup(item_id)
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
                _pool_cleanup(item_id)
                log_event("issue.reset", item_id=item_id, repo=item.get("repo"),
                          issue_number=item.get("issue_number"), title=item.get("title"),
                          details={"from_list": lst_name})
                print(f"REMOVED: {item_id} from {lst_name}")
                return True
    print(f"NOT FOUND: {item_id}")
    return False


def prioritize_top(item_id):
    """Move a pending item to the top of the queue (position 0)."""
    queue = load_queue()
    for i, item in enumerate(queue["pending"]):
        if item["id"] == item_id:
            queue["pending"].remove(item)
            queue["pending"].insert(0, item)
            save_queue(queue)
            log_event("issue.prioritized", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"action": "prioritize_top"})
            print(f"PRIORITIZED TOP: {item_id}")
            return True
    print(f"NOT FOUND in pending: {item_id}")
    return False


def move_down(item_id):
    """Move a pending item down one position in the queue."""
    queue = load_queue()
    for i, item in enumerate(queue["pending"]):
        if item["id"] == item_id:
            if i >= len(queue["pending"]) - 1:
                print(f"ALREADY AT BOTTOM: {item_id}")
                return False
            queue["pending"][i], queue["pending"][i + 1] = queue["pending"][i + 1], queue["pending"][i]
            save_queue(queue)
            log_event("issue.prioritized", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"action": "move_down"})
            print(f"MOVED DOWN: {item_id}")
            return True
    print(f"NOT FOUND in pending: {item_id}")
    return False


def move_to_bottom(item_id):
    """Move a pending item to the very bottom of the queue."""
    queue = load_queue()
    for i, item in enumerate(queue["pending"]):
        if item["id"] == item_id:
            if i >= len(queue["pending"]) - 1:
                print(f"ALREADY AT BOTTOM: {item_id}")
                return False
            queue["pending"].pop(i)
            queue["pending"].append(item)
            save_queue(queue)
            log_event("issue.prioritized", item_id=item_id, repo=item.get("repo"),
                      issue_number=item.get("issue_number"), title=item.get("title"),
                      details={"action": "move_to_bottom"})
            print(f"MOVED TO BOTTOM: {item_id}")
            return True
    print(f"NOT FOUND in pending: {item_id}")
    return False


def verify_issue_closed(repo, issue_number):
    """Quick check if a single issue is closed on GitHub."""
    import subprocess
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/issues/{issue_number}", "--jq", ".state"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0 and result.stdout.strip() == "closed"
    except Exception:
        return False


def auto_close_item(item_id, source="sync_prune"):
    """Move an item from any active queue list to completed (sync auto-close)."""
    queue = load_queue()
    for list_name in ["pending", "in_progress", "failed"]:
        for item in queue[list_name]:
            if item["id"] == item_id:
                item["completed_at"] = datetime.now(timezone.utc).isoformat()
                item["closed_via"] = source
                queue[list_name].remove(item)
                queue["completed"].append(item)
                save_queue(queue)
                _pool_cleanup(item_id)
                return True
    return False


def sync_github_issues(repo_full_name=None):
    """Incremental sync with GitHub.

    Phase 1 (first sync per repo): Fetch all open issues → add new, detect closed.
    Phase 2 (incremental): Fetch only issues updated since last sync → add new, prune closed.

    Returns structured result:
      {repos: {repo: {added: [...], closed: [...], unchanged: N, errors: [...]}},
       totals: {added: N, closed: N, unchanged: N}}
    """
    import subprocess

    sync_state = load_sync_state()
    queue = load_queue()
    existing_ids = set(
        x["id"] for x in
        queue["pending"] + queue["in_progress"] + queue["completed"] + queue["failed"]
    )

    if repo_full_name:
        repos = [repo_full_name]
    else:
        queued_repos = {
            x["repo"] for x in
            queue["pending"] + queue["in_progress"] + queue["completed"] + queue["failed"]
        }
        # Include sync-state repos for continuity and discover all active org repos
        # so newly-created repos with no existing queue items are scanned.
        repos = sorted(queued_repos | set(sync_state) | set(discover_org_repos()))

    sync_result = {"repos": {}, "totals": {"added": 0, "closed": 0, "unchanged": 0}}
    now = datetime.now(timezone.utc)
    skip_labels = {"question", "discussion", "wontfix", "duplicate", "invalid", "docs"}

    for repo in repos:
        repo_result = {"added": [], "closed": [], "unchanged": 0, "errors": []}
        last_sync = sync_state.get(repo)
        is_first_sync = last_sync is None

        try:
            timeout = 120 if is_first_sync else 30

            issues_by_number = {}
            urls = []
            if repo in visible_unassigned_repos():
                # Dashboard visibility: include all open issues for explicitly opted-in repos.
                urls.append(f"repos/{repo}/issues?state=open&per_page=100")
            else:
                # Default safety: only bring assigned issues onto the board.
                for assignee in sorted(allowed_assignees()):
                    urls.append(f"repos/{repo}/issues?state=open&per_page=100&assignee={assignee}")

            for url in urls:
                result = subprocess.run(
                    ["gh", "api", "--paginate", url, "--jq",
                     '.[] | select(.pull_request == null)'
                     '| {number, state, title, body, html_url, user: .user.login,'
                     '   labels: [.labels[].name], assignees: [.assignees[].login], updated_at}'],
                    capture_output=True, text=True, timeout=timeout
                )

                if result.returncode != 0:
                    repo_result["errors"].append(result.stderr[:200])
                    continue

                for line in result.stdout.strip().split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            issue = json.loads(line)
                            issues_by_number[issue["number"]] = issue
                        except json.JSONDecodeError:
                            continue

            issues = list(issues_by_number.values())
            if repo_result["errors"] and not issues:
                sync_result["repos"][repo] = repo_result
                continue

            open_by_number = {i["number"]: i for i in issues if i.get("state") == "open"}
            closed_by_number = {i["number"]: i for i in issues if i.get("state") == "closed"}

            # Full open-assigned set used ONLY for safe pruning. Incremental sync
            # may omit unchanged assigned issues, so never infer unassignment from
            # the incremental result alone.
            assigned_open_numbers = set(open_by_number)
            if not is_first_sync:
                full_assigned_open = {}
                for assignee in sorted(allowed_assignees()):
                    full_url = f"repos/{repo}/issues?state=open&per_page=100&assignee={assignee}"
                    full_result = subprocess.run(
                        ["gh", "api", "--paginate", full_url, "--jq",
                         '.[] | select(.pull_request == null)'
                         '| {number, state, title, body, html_url, user: .user.login,'
                         '   labels: [.labels[].name], assignees: [.assignees[].login], updated_at}'],
                        capture_output=True, text=True, timeout=120
                    )
                    if full_result.returncode != 0:
                        repo_result["errors"].append(full_result.stderr[:200])
                        continue
                    for line in full_result.stdout.strip().split("\n"):
                        line = line.strip()
                        if line:
                            try:
                                issue = json.loads(line)
                                full_assigned_open[issue["number"]] = issue
                            except json.JSONDecodeError:
                                continue
                assigned_open_numbers = set(full_assigned_open)
                for num, issue in full_assigned_open.items():
                    open_by_number.setdefault(num, issue)

            # --- Phase 1: Enqueue new open issues ---
            for num, issue in open_by_number.items():
                item_id = f"{repo}#{num}"
                labels = issue.get("labels", [])
                if any(l.lower() in skip_labels for l in labels):
                    repo_result["unchanged"] += 1
                    continue

                enqueued = enqueue(
                    repo=repo,
                    issue_number=issue["number"],
                    title=issue.get("title", ""),
                    body=issue.get("body", "") or "",
                    author=issue.get("user", ""),
                    labels=labels,
                    html_url=issue.get("html_url", ""),
                    assignees=issue.get("assignees", []),
                    require_allowed_assignee=repo not in visible_unassigned_repos(),
                )
                if enqueued:
                    repo_result["added"].append(item_id)
                    existing_ids.add(item_id)
                    log_event("issue.synced", item_id=item_id, repo=repo,
                              issue_number=issue["number"], title=issue.get("title", ""),
                              details={"source": "github_sync", "labels": labels})
                else:
                    repo_result["unchanged"] += 1

            # --- Phase 2: Prune closed issues ---
            queue = load_queue()  # reload after enqueues

            items_to_close = []
            for item in queue["pending"] + queue["in_progress"] + queue["failed"]:
                if item["repo"] != repo:
                    continue
                issue_num = item["issue_number"]

                if issue_num in closed_by_number:
                    # Confirmed closed in this sync window
                    items_to_close.append(item)
                elif is_first_sync and issue_num not in open_by_number:
                    # First sync: not in open set → verify individually
                    if verify_issue_closed(repo, issue_num):
                        items_to_close.append(item)

            # Visibility is separate from dispatch eligibility. Keep unassigned
            # items in pending so they appear in Mission Control; next_pending()
            # refuses to claim them until assigned to an allowed user.

            for item in items_to_close:
                auto_close_item(item["id"], source="sync_prune")
                repo_result["closed"].append(item["id"])
                log_event("issue.sync_pruned", item_id=item["id"], repo=repo,
                          issue_number=item["issue_number"], title=item.get("title", ""),
                          details={"source": "github_sync", "reason": "issue_closed"})

            # Update sync state for this repo
            sync_state[repo] = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        except subprocess.TimeoutExpired:
            repo_result["errors"].append("Timeout fetching from GitHub")
        except Exception as e:
            repo_result["errors"].append(str(e)[:200])

        sync_result["repos"][repo] = repo_result

    # Calculate totals
    for repo, rr in sync_result["repos"].items():
        sync_result["totals"]["added"] += len(rr["added"])
        sync_result["totals"]["closed"] += len(rr["closed"])
        sync_result["totals"]["unchanged"] += rr["unchanged"]

    save_sync_state(sync_state)

    t = sync_result["totals"]
    print(f"SYNC COMPLETE: {t['added']} added, {t['closed']} closed, {t['unchanged']} unchanged")
    return sync_result


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
    elif cmd == "prioritize":
        item_id = sys.argv[2]
        prioritize_top(item_id)
    elif cmd == "move-down":
        item_id = sys.argv[2]
        move_down(item_id)
    elif cmd == "move-to-bottom":
        item_id = sys.argv[2]
        move_to_bottom(item_id)
    elif cmd == "sync":
        repo = sys.argv[2] if len(sys.argv) > 2 else None
        results = sync_github_issues(repo)
        print(json.dumps(results, indent=2))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
