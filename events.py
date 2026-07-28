#!/usr/bin/env python3
"""
CWC Mission Control — Event Logger
SQLite-backed structured event log for the issue orchestrator system.

Every action (enqueue, claim, dispatch, complete, fail, guard trigger, etc.)
writes a row here. The dashboard reads from this for metrics, timeline, and
anomaly detection.
"""

import json
import sqlite3
import os
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(os.environ.get(
    "CWC_EVENTS_DB",
    str(Path.home() / ".hermes" / "issue-queue" / "events.db"),
))

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    event_type TEXT NOT NULL,
    item_id TEXT,
    repo TEXT,
    issue_number INTEGER,
    title TEXT,
    details TEXT DEFAULT '{}',
    source TEXT DEFAULT 'system'
);

CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_item_id ON events(item_id);
CREATE INDEX IF NOT EXISTS idx_events_repo ON events(repo);

CREATE TABLE IF NOT EXISTS metrics_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""


@contextmanager
def get_db():
    """Thread-safe DB connection with WAL mode for concurrent reads."""
    db = sqlite3.connect(str(DB_PATH), timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.row_factory = sqlite3.Row
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db():
    """Create tables and indexes if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript(SCHEMA)
    print(f"Event DB initialized at {DB_PATH}")


def log_event(event_type, item_id=None, repo=None, issue_number=None,
              title=None, details=None, source="system"):
    """
    Write an event to the log.

    Event types:
      issue.enqueued       — Issue added to pending queue
      issue.claimed        — Moved from pending → in_progress
      issue.completed      — Moved from in_progress → completed (with PR)
      issue.failed         — Moved from in_progress → failed
      issue.retried        — Moved from failed → pending
      issue.reset          — Removed from queue entirely
      issue.skipped        — Skipped (duplicate, skip label, linked PR)
      worker.dispatched    — Pi agent dispatched for an issue
      worker.completed     — Worker finished successfully
      worker.failed        — Worker failed
      webhook.received     — GitHub webhook payload received
      webhook.classified   — Issue classified as small/medium/large
      decompose.enqueued   — Large issue routed to decompose queue
      decompose.child_created — Child issue created from decomposition
      decompose.completed  — Decomposition finished
      decompose.failed     — Decomposition failed
      guard.triggered      — A guard/rule fired (over-decomposition, rate limit, etc.)
      system.info          — General system event
    """
    if details is None:
        details = {}
    elif not isinstance(details, dict):
        details = {"text": str(details)}

    with get_db() as db:
        db.execute(
            """INSERT INTO events (event_type, item_id, repo, issue_number, title, details, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event_type,
                item_id,
                repo,
                issue_number,
                title[:200] if title else None,
                json.dumps(details, default=str),
                source,
            ),
        )


def query_events(event_type=None, repo=None, item_id=None,
                 since=None, limit=100, offset=0):
    """Query events with filters. Returns list of dicts."""
    clauses = []
    params = []

    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if repo:
        clauses.append("repo = ?")
        params.append(repo)
    if item_id:
        clauses.append("item_id = ?")
        params.append(item_id)
    if since:
        clauses.append("timestamp >= ?")
        params.append(since)

    where = " AND ".join(clauses) if clauses else "1=1"

    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM events WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        return [dict(r) for r in rows]


def load_decompose_queue():
    """Return decompose queue from disk as dict."""
    from pathlib import Path
    p = Path.home() / ".hermes" / "issue-queue" / "decompose-queue.json"
    try:
        return json.load(open(p))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"pending": [], "completed": [], "failed": []}


def get_stats():
    """Get aggregate stats for the dashboard."""
    with get_db() as db:
        # Event counts by type (last 7 days)
        rows = db.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM events
            WHERE timestamp >= datetime('now', '-7 days')
            GROUP BY event_type
            ORDER BY cnt DESC
        """).fetchall()
        by_type = {r["event_type"]: r["cnt"] for r in rows}

        # Events per hour (last 24h)
        rows = db.execute("""
            SELECT strftime('%Y-%m-%dT%H:00:00Z', timestamp) as hour,
                   COUNT(*) as cnt
            FROM events
            WHERE timestamp >= datetime('now', '-24 hours')
            GROUP BY hour
            ORDER BY hour
        """).fetchall()
        hourly = [{"hour": r["hour"], "count": r["cnt"]} for r in rows]

        # Repo distribution (last 7 days)
        rows = db.execute("""
            SELECT repo, COUNT(*) as cnt
            FROM events
            WHERE timestamp >= datetime('now', '-7 days')
              AND repo IS NOT NULL
            GROUP BY repo
            ORDER BY cnt DESC
        """).fetchall()
        by_repo = {r["repo"]: r["cnt"] for r in rows}

        # Cycle times: time between issue.enqueued and issue.completed
        rows = db.execute("""
            SELECT
                e1.item_id,
                e1.timestamp as started,
                e2.timestamp as completed
            FROM events e1
            JOIN events e2 ON e1.item_id = e2.item_id
            WHERE e1.event_type = 'issue.enqueued'
              AND e2.event_type = 'issue.completed'
              AND e2.timestamp >= datetime('now', '-7 days')
        """).fetchall()
        cycle_times = []
        for r in rows:
            try:
                t1 = datetime.fromisoformat(r["started"].replace("Z", "+00:00"))
                t2 = datetime.fromisoformat(r["completed"].replace("Z", "+00:00"))
                minutes = (t2 - t1).total_seconds() / 60
                if minutes >= 0:
                    cycle_times.append(round(minutes, 1))
            except Exception:
                pass

        # Guard triggers (last 7 days)
        rows = db.execute("""
            SELECT details, COUNT(*) as cnt
            FROM events
            WHERE event_type = 'guard.triggered'
              AND timestamp >= datetime('now', '-7 days')
            GROUP BY details
            ORDER BY cnt DESC
            LIMIT 10
        """).fetchall()
        guards = [{"details": r["details"], "count": r["cnt"]} for r in rows]

        return {
            "events_by_type": by_type,
            "hourly_activity": hourly,
            "events_by_repo": by_repo,
            "cycle_times_minutes": cycle_times,
            "avg_cycle_time_minutes": round(sum(cycle_times) / len(cycle_times), 1) if cycle_times else None,
            "median_cycle_time_minutes": round(sorted(cycle_times)[len(cycle_times)//2], 1) if cycle_times else None,
            "guard_triggers": guards,
            "total_events_last_24h": sum(h["count"] for h in hourly),
        }


def get_decompose_tree():
    """Build parent→child tree from decompose events."""
    with get_db() as db:
        # Get all decompose child_created events
        rows = db.execute("""
            SELECT item_id, repo, issue_number, title, details, timestamp
            FROM events
            WHERE event_type = 'decompose.child_created'
            ORDER BY timestamp ASC
        """).fetchall()

        # Also get decompose.enqueued events for root epics
        roots = db.execute("""
            SELECT item_id, repo, issue_number, title, timestamp
            FROM events
            WHERE event_type = 'decompose.enqueued'
            ORDER BY timestamp ASC
        """).fetchall()

        tree = {}
        for r in roots:
            tree[r["item_id"]] = {
                "id": r["item_id"],
                "repo": r["repo"],
                "issue_number": r["issue_number"],
                "title": r["title"],
                "enqueued_at": r["timestamp"],
                "children": [],
            }

        for r in rows:
            details = json.loads(r["details"]) if r["details"] else {}
            parent_id = details.get("parent_id")
            child = {
                "id": r["item_id"],
                "repo": r["repo"],
                "issue_number": r["issue_number"],
                "title": r["title"],
                "created_at": r["timestamp"],
            }
            if parent_id and parent_id in tree:
                tree[parent_id]["children"].append(child)
            else:
                # Orphan child (parent not in tree) — still show
                orphan_id = f"orphan-{r['item_id']}"
                if orphan_id not in tree:
                    tree[orphan_id] = {
                        "id": details.get("parent_id", "unknown"),
                        "title": f"Unknown parent → {r['title'][:40]}",
                        "children": [],
                    }
                tree[orphan_id]["children"].append(child)

        return list(tree.values())


# CLI interface for testing
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: events.py <init|stats|query|seed>")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "init":
        init_db()
    elif cmd == "stats":
        init_db()
        import json as j
        print(j.dumps(get_stats(), indent=2))
    elif cmd == "query":
        init_db()
        import json as j
        events = query_events(limit=20)
        print(j.dumps(events, indent=2))
    elif cmd == "seed":
        # Seed from existing queue.json and decompose-queue.json
        init_db()
        queue_file = Path.home() / ".hermes" / "issue-queue" / "queue.json"
        decompose_file = Path.home() / ".hermes" / "issue-queue" / "decompose-queue.json"

        if queue_file.exists():
            q = json.load(open(queue_file))
            for item in q.get("completed", []):
                log_event(
                    "issue.enqueued",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "original_enqueued_at": item.get("enqueued_at")},
                    source="seed",
                )
                log_event(
                    "issue.completed",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "pr_number": item.get("pr_number"), "completed_at": item.get("completed_at")},
                    source="seed",
                )
            for item in q.get("pending", []):
                log_event(
                    "issue.enqueued",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "status": "pending"},
                    source="seed",
                )
            for item in q.get("in_progress", []):
                log_event(
                    "issue.enqueued",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "status": "in_progress"},
                    source="seed",
                )
                log_event(
                    "issue.claimed",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True},
                    source="seed",
                )
            for item in q.get("failed", []):
                log_event(
                    "issue.enqueued",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True},
                    source="seed",
                )
                log_event(
                    "issue.failed",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "error": item.get("error")},
                    source="seed",
                )
            print(f"Seeded {sum(len(q.get(k, [])) for k in ['pending','in_progress','completed','failed'])} items from queue.json")

        if decompose_file.exists():
            dq = json.load(open(decompose_file))
            for item in dq.get("completed", []):
                log_event(
                    "decompose.completed",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True},
                    source="seed",
                )
            for item in dq.get("failed", []):
                log_event(
                    "decompose.failed",
                    item_id=item["id"],
                    repo=item.get("repo"),
                    issue_number=item.get("issue_number"),
                    title=item.get("title"),
                    details={"seeded": True, "error": item.get("error")},
                    source="seed",
                )
            print(f"Seeded {len(dq.get('completed', []))} completed + {len(dq.get('failed', []))} failed from decompose-queue.json")
    else:
        print(f"Unknown command: {cmd}")
