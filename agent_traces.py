#!/usr/bin/env python3
"""Durable coding-agent trace metadata and bundle helpers.

This module is intentionally stdlib-only and independent of dispatch code. It
creates an agent_traces table in the Mission Control SQLite database and defines
the on-disk bundle convention used by future gateway/worker integrations.
"""

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


TRACE_FILES = (
    "meta.json",
    "prompt.md",
    "transcript.jsonl",
    "tool_calls.jsonl",
    "stdout.log",
    "final.txt",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_traces (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    repo TEXT,
    issue_number INTEGER,
    session_id TEXT,
    dispatch_id TEXT,
    pid INTEGER,
    status TEXT,
    started_at TEXT,
    finished_at TEXT,
    model_provider TEXT,
    model TEXT,
    prompt_id TEXT,
    pr_number INTEGER,
    exit_reason TEXT,
    log_path TEXT,
    transcript_path TEXT,
    trace_dir TEXT,
    error_summary TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_traces_item_id ON agent_traces(item_id);
CREATE INDEX IF NOT EXISTS idx_agent_traces_status ON agent_traces(status);
CREATE INDEX IF NOT EXISTS idx_agent_traces_updated_at ON agent_traces(updated_at);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def base_dir():
    return Path(os.environ.get("CWC_ISSUE_QUEUE_DIR", Path.home() / ".hermes" / "issue-queue"))


def db_path():
    return Path(os.environ.get("CWC_AGENT_TRACES_DB", base_dir() / "events.db"))


def traces_root():
    return Path(os.environ.get("CWC_AGENT_TRACES_DIR", base_dir() / "traces"))


def safe_item_id(item_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item_id or "").strip())
    return safe.strip("._") or "unknown"


def trace_dir_for_item(item_id):
    return traces_root() / safe_item_id(item_id)


def bundle_paths(item_id):
    trace_dir = trace_dir_for_item(item_id)
    paths = {"trace_dir": trace_dir}
    for name in TRACE_FILES:
        key = name.replace(".", "_")
        paths[key] = trace_dir / name
    return paths


def ensure_trace_bundle(item_id):
    paths = bundle_paths(item_id)
    paths["trace_dir"].mkdir(parents=True, exist_ok=True)
    return paths


def connect():
    db_file = db_path()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_file), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    init_db(db)
    return db


@contextmanager
def get_db():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(db=None):
    own = db is None
    if db is None:
        db_file = db_path()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(db_file), timeout=10)
    db.executescript(SCHEMA)
    db.commit()
    if own:
        db.close()


def row_to_dict(row):
    return dict(row) if row else None


def upsert_trace(**fields):
    """Insert or update a trace row.

    Callers may provide an id for idempotent updates. Without one, a UUID is
    generated. Only schema fields are persisted; updated_at is refreshed unless
    explicitly provided.
    """
    if not fields.get("item_id"):
        raise ValueError("item_id is required")

    allowed = {
        "id", "item_id", "repo", "issue_number", "session_id", "dispatch_id",
        "pid", "status", "started_at", "finished_at", "model_provider",
        "model", "prompt_id", "pr_number", "exit_reason", "log_path",
        "transcript_path", "trace_dir", "error_summary", "updated_at",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    data.setdefault("id", str(uuid.uuid4()))
    data.setdefault("trace_dir", str(trace_dir_for_item(data["item_id"])))
    data["updated_at"] = data.get("updated_at") or now_iso()

    columns = [
        "id", "item_id", "repo", "issue_number", "session_id", "dispatch_id",
        "pid", "status", "started_at", "finished_at", "model_provider",
        "model", "prompt_id", "pr_number", "exit_reason", "log_path",
        "transcript_path", "trace_dir", "error_summary", "updated_at",
    ]
    values = [data.get(col) for col in columns]
    updates = ", ".join(f"{col}=excluded.{col}" for col in columns if col != "id")

    with get_db() as db:
        db.execute(
            f"""
            INSERT INTO agent_traces ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(id) DO UPDATE SET {updates}
            """,
            values,
        )
        db.commit()
        row = db.execute("SELECT * FROM agent_traces WHERE id = ?", (data["id"],)).fetchone()
        return row_to_dict(row)


def get_trace(trace_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM agent_traces WHERE id = ?", (trace_id,)).fetchone()
        return row_to_dict(row)


def get_latest_trace_for_item(item_id):
    with get_db() as db:
        row = db.execute(
            """
            SELECT * FROM agent_traces
            WHERE item_id = ?
            ORDER BY updated_at DESC, started_at DESC, id DESC
            LIMIT 1
            """,
            (item_id,),
        ).fetchone()
        return row_to_dict(row)


def tail_text(path, max_bytes=12000):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()
        else:
            f.seek(0)
        return f.read().decode("utf-8", errors="replace")


def read_json_file(path):
    path = Path(path)
    if not path.exists() or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"_error": str(e)}


def bundle_status(item_id=None, trace_dir=None):
    trace_dir = Path(trace_dir) if trace_dir else trace_dir_for_item(item_id)
    files = {}
    for name in TRACE_FILES:
        path = trace_dir / name
        info = {
            "path": str(path),
            "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() and path.is_file() else 0,
        }
        if name == "meta.json":
            info["json"] = read_json_file(path)
        else:
            info["tail"] = tail_text(path)
        files[name] = info
    return {"trace_dir": str(trace_dir), "exists": trace_dir.exists(), "files": files}


def get_agent_trace_payload(item_id):
    trace = get_latest_trace_for_item(item_id)
    trace_dir = trace.get("trace_dir") if trace else trace_dir_for_item(item_id)
    bundle = bundle_status(item_id=item_id, trace_dir=trace_dir)
    return {
        "item_id": item_id,
        "trace": trace,
        "bundle": bundle,
        "has_trace": bool(trace),
        "has_bundle": bool(bundle.get("exists")),
    }


if __name__ == "__main__":
    init_db()
    print(json.dumps({"ok": True, "db": str(db_path()), "traces_dir": str(traces_root())}, indent=2))
