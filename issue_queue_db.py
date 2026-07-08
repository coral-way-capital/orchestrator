#!/usr/bin/env python3
"""SQLite compatibility ledger for CWC Mission Control issue queue.

The historical source of truth is queue.json because the dashboard and several
ops scripts depend on it. This module adds a transactional SQLite ledger that is
kept in sync on every queue save and gives the new dispatcher stable state,
priority, attempts, leases, and dispatch telemetry without breaking legacy JSON.
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dispatch_telemetry import normalize_dispatch_telemetry

BASE_DIR = Path.home() / ".hermes" / "issue-queue"
DB_FILE = BASE_DIR / "queue-state.db"
QUEUE_FILE = BASE_DIR / "queue.json"

STATUSES = ("pending", "in_progress", "completed", "failed")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def connect():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_FILE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    init_db(db)
    return db


def init_db(db=None):
    own = db is None
    db = db or sqlite3.connect(DB_FILE)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS issues (
            id TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            title TEXT,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 50,
            labels_json TEXT NOT NULL DEFAULT '[]',
            assignees_json TEXT NOT NULL DEFAULT '[]',
            html_url TEXT,
            enqueued_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            pr_number INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0,
            prompt_id TEXT,
            model_provider TEXT,
            model_name TEXT,
            dispatch_id TEXT,
            session_id TEXT,
            pid INTEGER,
            lease_expires_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_issues_status_priority ON issues(status, priority DESC, enqueued_at ASC);
        CREATE INDEX IF NOT EXISTS idx_issues_repo_status ON issues(repo, status);
        CREATE TABLE IF NOT EXISTS dispatches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id TEXT NOT NULL,
            repo TEXT NOT NULL,
            issue_number INTEGER NOT NULL,
            prompt_id TEXT,
            model_provider TEXT,
            model_name TEXT,
            dispatch_id TEXT,
            session_id TEXT,
            pid INTEGER,
            status TEXT NOT NULL,
            response_json TEXT,
            created_at TEXT NOT NULL
        );
        """
    )
    _ensure_columns(db, "issues", {
        "log_path": "TEXT",
        "transcript_path": "TEXT",
        "status_url": "TEXT",
        "status_path": "TEXT",
        "telemetry_missing_json": "TEXT NOT NULL DEFAULT '[]'",
        "liveness_reliable": "INTEGER NOT NULL DEFAULT 0",
    })
    _ensure_columns(db, "dispatches", {
        "log_path": "TEXT",
        "transcript_path": "TEXT",
        "status_url": "TEXT",
        "status_path": "TEXT",
        "telemetry_missing_json": "TEXT NOT NULL DEFAULT '[]'",
        "liveness_reliable": "INTEGER NOT NULL DEFAULT 0",
    })
    db.commit()
    if own:
        db.close()


def _ensure_columns(db, table, columns):
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


PRIORITY_LABEL_SCORES = {
    "priority:critical": 100,
    "priority:p0": 100,
    "p0": 100,
    "critical": 100,
    "sev0": 100,
    "sev1": 100,
    "priority:high": 85,
    "priority:p1": 85,
    "p1": 85,
    "high": 85,
    "priority:medium": 65,
    "priority:p2": 65,
    "p2": 65,
    "medium": 65,
    "priority:low": 35,
    "priority:p3": 35,
    "p3": 35,
    "low": 35,
}


def compute_priority(item):
    labels = {str(x).lower() for x in item.get("labels", [])}
    title = (item.get("title") or "").lower()
    body = (item.get("body") or "").lower()

    explicit = [PRIORITY_LABEL_SCORES[label] for label in labels if label in PRIORITY_LABEL_SCORES]
    if explicit:
        score = max(explicit)
    else:
        score = 50
        if labels & {"bug"}:
            score += 25
        if labels & {"client", "customer", "production", "prod"}:
            score += 20
        if labels & {"docs", "question", "discussion", "documentation"}:
            score -= 10

    if any(k in title for k in ("bug", "broken", "production", "prod", "cliente", "client")):
        score += 10
    if any(k in body for k in ("acceptance criteria", "criterios de aceptación")):
        score += 5
    return max(0, min(100, score))


def issue_order_value(item):
    """Stable definition order: lower GitHub issue number means defined earlier."""
    try:
        number = item.get("issue_number")
        return int(number) if number is not None else 10**9
    except (TypeError, ValueError):
        return 10**9


def priority_sort_key(item):
    """Sort key for dispatch: explicit priority first, then first-defined order."""
    return (-compute_priority(item), issue_order_value(item), item.get("enqueued_at") or "")


def upsert_issue(db, item, status):
    labels = json.dumps(item.get("labels", []), ensure_ascii=False)
    assignees = json.dumps(item.get("assignees", []), ensure_ascii=False)
    db.execute(
        """
        INSERT INTO issues (
          id, repo, issue_number, title, status, priority, labels_json,
          assignees_json, html_url, enqueued_at, started_at, completed_at,
          pr_number, prompt_id, dispatch_id, session_id, pid, log_path,
          transcript_path, status_url, status_path, telemetry_missing_json,
          liveness_reliable, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          repo=excluded.repo,
          issue_number=excluded.issue_number,
          title=excluded.title,
          status=excluded.status,
          priority=excluded.priority,
          labels_json=excluded.labels_json,
          assignees_json=excluded.assignees_json,
          html_url=excluded.html_url,
          enqueued_at=excluded.enqueued_at,
          started_at=excluded.started_at,
          completed_at=excluded.completed_at,
          pr_number=excluded.pr_number,
          prompt_id=COALESCE(excluded.prompt_id, issues.prompt_id),
          dispatch_id=COALESCE(excluded.dispatch_id, issues.dispatch_id),
          session_id=COALESCE(excluded.session_id, issues.session_id),
          pid=COALESCE(excluded.pid, issues.pid),
          log_path=COALESCE(excluded.log_path, issues.log_path),
          transcript_path=COALESCE(excluded.transcript_path, issues.transcript_path),
          status_url=COALESCE(excluded.status_url, issues.status_url),
          status_path=COALESCE(excluded.status_path, issues.status_path),
          telemetry_missing_json=excluded.telemetry_missing_json,
          liveness_reliable=excluded.liveness_reliable,
          updated_at=excluded.updated_at
        """,
        (
            item.get("id"), item.get("repo"), item.get("issue_number"), item.get("title"),
            status, compute_priority(item), labels, assignees, item.get("html_url"),
            item.get("enqueued_at"), item.get("started_at"), item.get("completed_at"),
            item.get("pr_number"), item.get("agent_prompt"), item.get("dispatch_id"),
            item.get("session_id"), item.get("agent_pid"), item.get("log_path"),
            item.get("transcript_path"), item.get("status_url"), item.get("status_path"),
            json.dumps(item.get("telemetry_missing", []), ensure_ascii=False),
            1 if item.get("liveness_reliable") else 0, now_iso(),
        ),
    )


def sync_from_queue(queue=None):
    if queue is None:
        if not QUEUE_FILE.exists():
            queue = {s: [] for s in STATUSES}
        else:
            queue = json.loads(QUEUE_FILE.read_text())
    with connect() as db:
        seen = set()
        for status in STATUSES:
            for item in queue.get(status, []):
                seen.add(item.get("id"))
                upsert_issue(db, item, status)
        if seen:
            placeholders = ",".join("?" for _ in seen)
            db.execute(f"DELETE FROM issues WHERE id NOT IN ({placeholders})", tuple(seen))
        db.commit()


def record_dispatch(item, prompt_id, model_provider, model_name, response=None, status="accepted"):
    response = response or {}
    telemetry = normalize_dispatch_telemetry(response)
    dispatch_id = telemetry.get("dispatch_id")
    session_id = telemetry.get("session_id")
    pid = telemetry.get("pid")
    with connect() as db:
        db.execute(
            """
            INSERT INTO dispatches(item_id, repo, issue_number, prompt_id, model_provider,
              model_name, dispatch_id, session_id, pid, log_path, transcript_path,
              status_url, status_path, telemetry_missing_json, liveness_reliable,
              status, response_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (item.get("id"), item.get("repo"), item.get("issue_number"), prompt_id,
             model_provider, model_name, dispatch_id, session_id, pid,
             telemetry.get("log_path"), telemetry.get("transcript_path"),
             telemetry.get("status_url"), telemetry.get("status_path"),
             json.dumps(telemetry.get("telemetry_missing", []), ensure_ascii=False),
             1 if telemetry.get("liveness_reliable") else 0,
             status, json.dumps(response, ensure_ascii=False, default=str), now_iso()),
        )
        db.execute(
            """
            UPDATE issues SET attempts=attempts+1, prompt_id=?, model_provider=?, model_name=?,
              dispatch_id=COALESCE(?, dispatch_id), session_id=COALESCE(?, session_id),
              pid=COALESCE(?, pid), log_path=COALESCE(?, log_path),
              transcript_path=COALESCE(?, transcript_path), status_url=COALESCE(?, status_url),
              status_path=COALESCE(?, status_path), telemetry_missing_json=?,
              liveness_reliable=?, updated_at=? WHERE id=?
            """,
            (prompt_id, model_provider, model_name, dispatch_id, session_id, pid,
             telemetry.get("log_path"), telemetry.get("transcript_path"),
             telemetry.get("status_url"), telemetry.get("status_path"),
             json.dumps(telemetry.get("telemetry_missing", []), ensure_ascii=False),
             1 if telemetry.get("liveness_reliable") else 0,
             now_iso(), item.get("id")),
        )
        db.commit()
    return telemetry


if __name__ == "__main__":
    sync_from_queue()
    with connect() as db:
        counts = dict(db.execute("SELECT status, COUNT(*) c FROM issues GROUP BY status").fetchall())
    print(json.dumps({"ok": True, "db": str(DB_FILE), "counts": counts}, indent=2))
