#!/usr/bin/env python3
"""Versioned structured worker-result contract and idempotent receiver.

A worker terminal result is the authoritative outcome of a dispatch: it tells
Mission Control whether the agent succeeded (and the PR number), failed (and the
error), or could not be determined. Historically these outcomes were only
recovered by scraping a 2 MB tail of ``~/.hermes/logs/gateway.log`` (see
``gateway_reconciler.py``), which lost ~40% of outcomes because the relevant
final line rolled out of the tail.

This module defines:

* ``WORKER_RESULT_VERSION`` — a versioned schema identifier for the contract.
* ``TERMINAL_STATUSES`` — the allowed terminal outcome values.
* ``validate_worker_result(payload)`` — validate + normalize a raw contract.
* ``ingest_worker_result(payload, *, source=...)`` — idempotently persist a
  terminal result into the queue, the SQLite ledger, and agent_traces, with
  structured event logging. Duplicate results for the same dispatch are safe:
  the first terminal result wins, later duplicates are audited but do not
  mutate queue state.
* ``find_in_progress_by_dispatch_id`` / ``apply_outcome`` — queue mutation used
  by both the receiver and the reconciler fallback path.
* ``scan_session_files`` — session-file fallback: when no structured result and
  no gateway-log line exist, the on-disk trace bundle's ``final.txt`` /
  ``meta.json`` may still hold evidence.

Contract (v1)
-------------
Required fields::

    {
      "version": 1,
      "dispatch_id": "webhook:cwc-issue-dispatch:<delivery_id>",
      "item_id": "coral-way-capital/audit-agent#42",
      "status": "completed" | "failed" | "unknown",
      "occurred_at": "2026-07-27T21:00:00+00:00",
    }

Status-specific fields:

* ``completed`` → ``pr_number`` (int, optional but strongly expected; absent
  means "completed but no PR detected yet").
* ``failed``    → ``error_summary`` (str).
* ``unknown``   → ``error_summary`` (str, optional) describing why the outcome
  could not be determined. Unknown is a terminal preservation state: it never
  infers success and keeps the item queryable for backfill.

Optional fields: ``repo``, ``issue_number``, ``session_id``, ``evidence``
(free-form dict of URLs, SHAs, log excerpts), and exact ``telemetry``. Telemetry
accepts provider-response usage, auditable cost, and independently sourced
accepted-outcome metadata; unsupported values remain ``not_available``.

Idempotency rules
-----------------
1. The natural key is ``dispatch_id``. Re-ingesting the same dispatch_id is
   always safe.
2. Once a dispatch has a *resolved* terminal status (completed/failed), a later
   result with a different status is recorded as a ``worker_result.duplicate``
   audit event but does NOT move the item. This prevents flapping.
3. ``unknown`` never blocks a later resolved result.
4. Every ingest writes a row (``worker_result.received`` for new, and
   ``worker_result.duplicate`` for repeats) so duplicate delivery is auditable.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import fcntl

from dispatch_telemetry import normalize_terminal_telemetry, record_terminal_result

WORKER_RESULT_VERSION = 1
TERMINAL_STATUSES = ("completed", "failed", "unknown")
RESOLVED_STATUSES = ("completed", "failed")

# Dispatch id shape produced by the gateway: webhook:cwc-issue-dispatch:<id>.
# We key idempotency on this; if it is absent we fall back to item_id.
DISPATCH_ID_RE = re.compile(r"^webhook:cwc-issue-dispatch:[^:\s]+$")
ITEM_ID_RE = re.compile(
    r"^(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)$"
)

# Default location for the results ledger. Mirrors issue_queue_db / events.
BASE_DIR = Path.home() / ".hermes" / "issue-queue"
RESULTS_DB = BASE_DIR / "queue-state.db"  # reuse the same ledger DB


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Database schema (lives in the existing queue-state.db ledger)
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id TEXT,
    item_id TEXT NOT NULL,
    repo TEXT,
    issue_number INTEGER,
    session_id TEXT,
    status TEXT NOT NULL,
    pr_number INTEGER,
    error_summary TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'worker',
    occurred_at TEXT,
    received_at TEXT NOT NULL,
    UNIQUE(dispatch_id, status, source)
);
CREATE INDEX IF NOT EXISTS idx_worker_results_dispatch_id ON worker_results(dispatch_id);
CREATE INDEX IF NOT EXISTS idx_worker_results_item_id ON worker_results(item_id);
CREATE INDEX IF NOT EXISTS idx_worker_results_status ON worker_results(status);
"""


def _connect(db_path: Path | str = RESULTS_DB):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def get_db(db_path: Path | str = RESULTS_DB):
    db = _connect(db_path)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db(db_path: Path | str = RESULTS_DB) -> None:
    own = True
    try:
        _connect(db_path).close()
    except Exception:
        own = False
    _ = own


# --------------------------------------------------------------------------- #
# Validation / normalization
# --------------------------------------------------------------------------- #

class WorkerResultError(ValueError):
    """Raised when a worker-result payload violates the contract."""


def _require(value: Any, name: str) -> Any:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        raise WorkerResultError(f"{name} is required")
    return value


def validate_worker_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a raw worker-result payload.

    Raises ``WorkerResultError`` on contract violation. Returns a clean dict
    with only schema fields, ``status`` lowercased, ``pr_number`` coerced to
    int when present, and ``version`` defaulted to the current version.
    """
    if not isinstance(payload, dict):
        raise WorkerResultError("payload must be a JSON object")

    version = _require(payload.get("version"), "version")
    if isinstance(version, bool) or not isinstance(version, int) or version != WORKER_RESULT_VERSION:
        raise WorkerResultError(
            f"unsupported worker-result version {version!r}; expected {WORKER_RESULT_VERSION}"
        )

    dispatch_id = str(_require(payload.get("dispatch_id"), "dispatch_id")).strip()
    if len(dispatch_id) > 512 or not DISPATCH_ID_RE.fullmatch(dispatch_id):
        raise WorkerResultError(
            "dispatch_id must be a non-empty cwc-issue-dispatch identifier"
        )
    item_id = str(_require(payload.get("item_id"), "item_id")).strip()
    item_match = ITEM_ID_RE.fullmatch(item_id)
    if not item_match:
        raise WorkerResultError("item_id must have the form owner/repo#number")
    status = str(_require(payload.get("status"), "status")).strip().lower()
    if status not in TERMINAL_STATUSES:
        raise WorkerResultError(
            f"status must be one of {TERMINAL_STATUSES}; got {status!r}"
        )
    occurred_at = str(_require(payload.get("occurred_at"), "occurred_at")).strip()
    try:
        occurred = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerResultError("occurred_at must be an ISO-8601 timestamp") from exc
    if occurred.tzinfo is None or occurred.utcoffset() is None:
        raise WorkerResultError("occurred_at must include a timezone offset")

    repo = payload.get("repo")
    if repo is not None:
        if not isinstance(repo, str) or repo != item_match.group("repo"):
            raise WorkerResultError("repo must match item_id")
    issue_number = payload.get("issue_number")
    if issue_number is not None:
        if (
            isinstance(issue_number, bool)
            or not isinstance(issue_number, int)
            or issue_number != int(item_match.group("number"))
        ):
            raise WorkerResultError("issue_number must match item_id")
    evidence = payload.get("evidence", {})
    if not isinstance(evidence, dict):
        raise WorkerResultError("evidence must be a JSON object")
    if len(json.dumps(evidence, ensure_ascii=False, default=str).encode("utf-8")) > 65_536:
        raise WorkerResultError("evidence exceeds 65536 bytes")

    normalized: dict[str, Any] = {
        "version": WORKER_RESULT_VERSION,
        "dispatch_id": dispatch_id,
        "item_id": item_id,
        "repo": repo,
        "issue_number": issue_number,
        "session_id": payload.get("session_id"),
        "status": status,
        "pr_number": None,
        "error_summary": None,
        "evidence": evidence,
        "source": str(payload.get("source") or "worker"),
        "occurred_at": str(occurred_at),
    }
    try:
        normalized["telemetry"] = normalize_terminal_telemetry(payload.get("telemetry"))
    except ValueError as exc:
        raise WorkerResultError(str(exc)) from exc

    if status == "completed":
        pr_number = payload.get("pr_number")
        if pr_number not in (None, ""):
            try:
                normalized["pr_number"] = int(pr_number)
            except (TypeError, ValueError) as exc:
                raise WorkerResultError(f"pr_number must be an integer: {pr_number!r}") from exc
            if isinstance(pr_number, bool) or normalized["pr_number"] <= 0:
                raise WorkerResultError("pr_number must be a positive integer")
    elif status == "failed":
        normalized["error_summary"] = str(
            _require(payload.get("error_summary"), "error_summary (required for failed)")
        )[:1000]
    elif status == "unknown":
        summary = payload.get("error_summary")
        if summary:
            normalized["error_summary"] = str(summary)[:1000]

    return normalized


# --------------------------------------------------------------------------- #
# Ledger queries
# --------------------------------------------------------------------------- #

def get_latest_result(dispatch_id: str | None, item_id: str | None,
                      db_path: Path | str = RESULTS_DB) -> dict[str, Any] | None:
    """Return the most recent resolved (completed/failed) result for the key."""
    if not dispatch_id and not item_id:
        return None
    clauses, params = [], []
    if dispatch_id:
        clauses.append("dispatch_id = ?")
        params.append(dispatch_id)
    else:
        clauses.append("item_id = ?")
        params.append(item_id)
    where = " AND ".join(clauses)
    with get_db(db_path) as db:
        row = db.execute(
            f"""
            SELECT * FROM worker_results
            WHERE {where} AND status IN ('completed','failed')
            ORDER BY received_at ASC, id ASC LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        return dict(row) if row else None


def list_results(item_id: str | None = None, db_path: Path | str = RESULTS_DB) -> list[dict[str, Any]]:
    clauses, params = [], []
    if item_id:
        clauses.append("item_id = ?")
        params.append(item_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with get_db(db_path) as db:
        rows = db.execute(
            f"SELECT * FROM worker_results {where} ORDER BY received_at ASC, id ASC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def get_latest_result_any(dispatch_id: str, db_path: Path | str = RESULTS_DB) -> dict[str, Any] | None:
    """Return the latest result of any status for one dispatch."""
    with get_db(db_path) as db:
        row = db.execute(
            """
            SELECT * FROM worker_results
            WHERE dispatch_id = ?
            ORDER BY received_at DESC, id DESC LIMIT 1
            """,
            (dispatch_id,),
        ).fetchone()
        return dict(row) if row else None


def _insert_result(db: sqlite3.Connection, r: dict[str, Any]) -> int:
    cur = db.execute(
        """
        INSERT OR IGNORE INTO worker_results
          (dispatch_id, item_id, repo, issue_number, session_id, status,
           pr_number, error_summary, evidence_json, source, occurred_at, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            r.get("dispatch_id"), r.get("item_id"), r.get("repo"), r.get("issue_number"),
            r.get("session_id"), r.get("status"), r.get("pr_number"), r.get("error_summary"),
            json.dumps(r.get("evidence") or {}, ensure_ascii=False, default=str),
            r.get("source"), r.get("occurred_at"), r.get("received_at") or now_iso(),
        ),
    )
    return cur.rowcount


@contextmanager
def _ingest_lock(db_path: Path | str):
    """Serialize ledger + queue decisions across receiver threads/processes."""
    lock_path = Path(db_path).with_suffix(Path(db_path).suffix + ".worker-results.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _first_resolved(db: sqlite3.Connection, dispatch_id: str) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT * FROM worker_results
        WHERE dispatch_id = ? AND status IN ('completed','failed')
        ORDER BY received_at ASC, id ASC LIMIT 1
        """,
        (dispatch_id,),
    ).fetchone()
    return dict(row) if row else None


def _latest_any(db: sqlite3.Connection, dispatch_id: str) -> dict[str, Any] | None:
    row = db.execute(
        """
        SELECT * FROM worker_results
        WHERE dispatch_id = ?
        ORDER BY received_at DESC, id DESC LIMIT 1
        """,
        (dispatch_id,),
    ).fetchone()
    return dict(row) if row else None


def _same_outcome(previous: dict[str, Any], result: dict[str, Any]) -> bool:
    if previous.get("status") != result.get("status"):
        return False
    if result.get("status") == "completed":
        return previous.get("pr_number") == result.get("pr_number")
    if result.get("status") == "failed":
        return (previous.get("error_summary") or "") == (result.get("error_summary") or "")
    return True


# --------------------------------------------------------------------------- #
# Queue mutation helpers
# --------------------------------------------------------------------------- #

def _find_in_progress_item(queue: dict[str, Any], item_id: str) -> tuple[dict[str, Any] | None, str | None]:
    for bucket in ("in_progress", "pending", "failed", "completed"):
        for it in queue.get(bucket, []):
            if it.get("id") == item_id:
                return it, bucket
    return None, None


def _move_to_terminal(queue: dict[str, Any], item: dict[str, Any], source_bucket: str,
                      target_bucket: str) -> None:
    try:
        queue[source_bucket].remove(item)
    except (ValueError, KeyError):
        pass
    queue.setdefault(target_bucket, []).append(item)


def _terminal_trace_text(result: dict[str, Any]) -> str:
    if result.get("status") == "completed":
        pr_number = result.get("pr_number")
        return f"PR #{pr_number}" if pr_number else "completed"
    return result.get("error_summary") or result.get("status") or "unknown"


def _record_terminal_trace(item: dict[str, Any], result: dict[str, Any]) -> None:
    """Mirror structured terminal results into trace files and agent_traces."""
    try:
        from agent_traces import ensure_trace_bundle, get_latest_trace_for_item, upsert_trace

        item_id = item.get("id") or result.get("item_id")
        if not item_id:
            return
        finished_at = result.get("occurred_at") or result.get("received_at") or now_iso()
        trace_paths = ensure_trace_bundle(item_id)

        final_text = _terminal_trace_text(result)
        trace_paths["final_txt"].write_text(final_text, encoding="utf-8")

        meta_path = trace_paths["meta_json"]
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        meta.update({
            "status": result.get("status"),
            "finished_at": finished_at,
            "worker_result_received_at": result.get("received_at") or now_iso(),
            "worker_result_status": result.get("status"),
            "worker_result_dispatch_id": result.get("dispatch_id"),
            "worker_result_pr_number": result.get("pr_number"),
            "worker_result_error_summary": result.get("error_summary"),
            "worker_result_source": result.get("source"),
        })
        meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

        existing = get_latest_trace_for_item(item_id)
        fields = {
            "item_id": item_id,
            "repo": item.get("repo") or result.get("repo"),
            "issue_number": item.get("issue_number") or result.get("issue_number"),
            "session_id": item.get("session_id") or result.get("session_id"),
            "dispatch_id": result.get("dispatch_id") or _item_dispatch_id(item),
            "status": result.get("status"),
            "finished_at": finished_at,
            "pr_number": result.get("pr_number"),
            "exit_reason": "worker_result",
            "error_summary": result.get("error_summary"),
            "trace_dir": str(trace_paths["trace_dir"]),
        }
        if existing and existing.get("id"):
            fields["id"] = existing["id"]
            for key in (
                "started_at", "model_provider", "model", "prompt_id",
                "log_path", "transcript_path",
            ):
                if existing.get(key) is not None:
                    fields[key] = existing.get(key)
        upsert_trace(**fields)
    except Exception:
        pass


def _item_dispatch_id(item: dict[str, Any]) -> str | None:
    if item.get("dispatch_id"):
        return str(item["dispatch_id"])
    telemetry = item.get("telemetry")
    if isinstance(telemetry, dict) and telemetry.get("dispatch_id"):
        return str(telemetry["dispatch_id"])
    return None


def apply_outcome_to_queue(result: dict[str, Any], queue: dict[str, Any],
                           *, mutate: bool = True,
                           require_dispatch_match: bool = True) -> dict[str, Any]:
    """Apply a validated result to an in-memory queue dict.

    Returns a summary dict describing what (if anything) changed. When
    ``mutate`` is False this is a pure inspection used by the backfill report.

    Idempotency: if the item is already in the matching terminal bucket with a
    matching pr_number/error, this is a no-op (returns ``applied=False``).
    """
    status = result["status"]
    item_id = result["item_id"]
    item, bucket = _find_in_progress_item(queue, item_id)

    summary: dict[str, Any] = {
        "item_id": item_id,
        "status": status,
        "applied": False,
        "already_terminal": False,
        "skipped_unknown": False,
        "dispatch_mismatch": False,
        "from_bucket": bucket,
        "pr_number": result.get("pr_number"),
    }

    if status == "unknown":
        # Never infer success; record but do not move.
        summary["skipped_unknown"] = True
        return summary

    if item is None or bucket is None:
        return summary

    if bucket == "pending":
        summary["not_in_progress"] = True
        return summary

    current_dispatch_id = _item_dispatch_id(item)
    if bucket == "in_progress":
        if not current_dispatch_id and require_dispatch_match:
            summary["dispatch_unverifiable"] = True
            return summary
        if current_dispatch_id and current_dispatch_id != result.get("dispatch_id"):
            summary["dispatch_mismatch"] = True
            return summary

    if bucket in ("completed", "failed"):
        # Already terminal. Detect no-op duplicates vs conflicting duplicates.
        same = (
            (bucket == "completed" and status == "completed"
             and item.get("pr_number") == result.get("pr_number"))
            or (bucket == "failed" and status == "failed")
        )
        summary["already_terminal"] = True
        summary["applied"] = False
        summary["same_outcome"] = same
        return summary

    if not mutate:
        return summary

    if status == "completed":
        item["completed_at"] = now_iso()
        item["pr_number"] = result.get("pr_number")
        item.pop("error", None)
        _move_to_terminal(queue, item, bucket, "completed")
    else:  # failed
        item["completed_at"] = now_iso()
        item["error"] = (result.get("error_summary") or "worker failed")[:500]
        _move_to_terminal(queue, item, bucket, "failed")

    summary["applied"] = True
    summary["to_bucket"] = "completed" if status == "completed" else "failed"
    return summary



# --------------------------------------------------------------------------- #
# Public ingest entrypoint
# --------------------------------------------------------------------------- #

def ingest_worker_result(payload: dict[str, Any], *, source: str = "worker",
                         db_path: Path | str = RESULTS_DB,
                         queue_loader=None, queue_saver=None,
                         event_logger=None,
                         telemetry_db_path: Path | str | None = None) -> dict[str, Any]:
    """Validate, persist, and apply a worker-result payload.

    Idempotent: duplicate delivery is safe and audited. Returns a dict with::

        {ok, duplicate, applied, already_terminal, result, summary}

    Callers may inject ``queue_loader``/``queue_saver`` (defaults to
    ``queue.load_queue``/``queue.save_queue``) and ``event_logger`` (defaults to
    ``events.log_event``) for testing.
    """
    payload = dict(payload or {})
    # Provenance is assigned by the trusted caller, never by submitted JSON.
    payload["source"] = source
    result = validate_worker_result(payload)

    received_at = now_iso()
    result["received_at"] = received_at

    dispatch_id = result.get("dispatch_id")
    item_id = result["item_id"]

    if queue_loader is None:
        import queue as queue_mod  # type: ignore[import-not-found]
        queue_loader = queue_mod.load_queue
    if queue_saver is None:
        import queue as queue_mod  # type: ignore[import-not-found]
        queue_saver = queue_mod.save_queue
    if event_logger is None:
        try:
            from events import log_event as event_logger  # type: ignore[assignment]
        except Exception:
            def event_logger(*a, **kw):  # type: ignore[misc]
                pass

    # Serialize the first-terminal-wins decision. The result ledger shares its
    # SQLite file with queue.save_queue's compatibility sync, so its write
    # transaction must commit before queue_saver opens a second connection.
    # Redelivery repairs either possible crash boundary: a ledger-only result
    # may re-apply the same outcome, while an already-terminal queue item
    # accepts the missing ledger row without another mutation.
    with _ingest_lock(db_path):
        db = _connect(db_path)
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = _first_resolved(db, dispatch_id)
            previous = _latest_any(db, dispatch_id)
            is_duplicate_resolved = bool(existing)
            same_outcome = bool(existing and _same_outcome(existing, result))
            conflict = bool(existing and not same_outcome)
            is_exact_duplicate = bool(previous and _same_outcome(previous, result))

            _insert_result(db, result)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        queue = queue_loader()
        dispatch_context, _ = _find_in_progress_item(queue, item_id)
        dispatch_context = dict(dispatch_context or {})
        # A same-outcome redelivery may repair a ledger/queue partial state. A
        # conflicting terminal result never mutates the queue.
        summary = apply_outcome_to_queue(
            result, queue, mutate=not conflict
        )
        if summary.get("applied"):
            queue_saver(queue)

        should_record_trace = bool(summary.get("applied")) or not (
            conflict
            or is_exact_duplicate
            or summary.get("already_terminal")
            or summary.get("dispatch_mismatch")
            or summary.get("dispatch_unverifiable")
        )
        if should_record_trace:
            trace_item, _ = _find_in_progress_item(queue, item_id)
            if trace_item is not None:
                _record_terminal_trace(trace_item, result)
        if not conflict:
            record_terminal_result(
                result,
                dispatch_context=dispatch_context,
                db_path=telemetry_db_path or db_path,
            )

    if summary.get("applied"):
        try:
            import queue as queue_mod  # type: ignore[import-not-found]
            queue_mod._pool_cleanup(item_id)
        except Exception:
            pass

    # Auditable event log. Always emit something so duplicate delivery is visible.
    event_type = "worker_result.duplicate" if (
        is_duplicate_resolved or is_exact_duplicate or summary.get("already_terminal")
    ) else "worker_result.received"
    event_logger(
        event_type,
        item_id=item_id,
        repo=result.get("repo"),
        issue_number=result.get("issue_number"),
        details={
            "dispatch_id": dispatch_id,
            "status": result["status"],
            "pr_number": result.get("pr_number"),
            "error_summary": result.get("error_summary"),
            "source": result.get("source"),
            "occurred_at": result.get("occurred_at"),
            "received_at": received_at,
            "applied": summary.get("applied"),
            "already_terminal": summary.get("already_terminal"),
            "same_outcome": summary.get("same_outcome"),
            "conflict": conflict,
            "version": WORKER_RESULT_VERSION,
        },
        source=result.get("source") or "worker",
    )

    return {
        "ok": True,
        "duplicate": bool(
            is_duplicate_resolved or is_exact_duplicate or summary.get("already_terminal")
        ),
        "same_outcome": summary.get("same_outcome"),
        "conflict": conflict,
        "applied": summary.get("applied"),
        "already_terminal": summary.get("already_terminal"),
        "result": _public_result(result),
        "summary": summary,
    }


def _public_result(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": r["version"],
        "dispatch_id": r.get("dispatch_id"),
        "item_id": r["item_id"],
        "status": r["status"],
        "pr_number": r.get("pr_number"),
        "error_summary": r.get("error_summary"),
        "occurred_at": r.get("occurred_at"),
    }


# --------------------------------------------------------------------------- #
# Session-file fallback
# --------------------------------------------------------------------------- #

def _trace_final_text(trace_dir: Path) -> str | None:
    final = trace_dir / "final.txt"
    if final.exists() and final.is_file():
        try:
            return final.read_text(encoding="utf-8").strip() or None
        except Exception:
            return None
    return None


def _trace_meta_status(trace_dir: Path) -> str | None:
    meta_path = trace_dir / "meta.json"
    if not meta_path.exists() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    for key in ("reconciled_status", "status"):
        val = meta.get(key)
        if val in TERMINAL_STATUSES:
            return val
    return None


def scan_session_files(traces_root: Path, queue: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Best-effort fallback: derive candidate outcomes from trace bundles.

    For each in_progress item with a trace directory containing ``final.txt`` or
    a ``meta.json`` status, attempt to build a synthetic worker result. This is
    a *fallback* source only — the structured receiver is always preferred.
    """
    import queue as queue_mod  # local import for default  # noqa: F401
    if queue is None:
        try:
            import queue as queue_mod  # type: ignore[import-not-found]
            queue = queue_mod.load_queue()
        except Exception:
            queue = {}

    candidates: list[dict[str, Any]] = []
    for item in list(queue.get("in_progress", [])):
        item_id = item.get("id")
        if not item_id:
            continue
        # Mirror agent_traces.safe_item_id naming.
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item_id)).strip("._") or "unknown"
        trace_dir = Path(traces_root) / safe
        if not trace_dir.exists():
            continue
        status = _trace_meta_status(trace_dir)
        final_text = _trace_final_text(trace_dir)
        if not status and final_text:
            low = final_text.lower()
            if low.startswith(("failed", "error:")):
                status = "failed"
            else:
                # A PR mention is not proof that the dispatch succeeded: it
                # can describe pending, failed, or unrelated work.
                status = "unknown"
        if status is None:
            continue
        pr_number = None
        if status == "completed" and final_text:
            m = re.search(r"PR\s*#(\d+)", final_text, re.IGNORECASE)
            if m:
                pr_number = int(m.group(1))
        candidates.append({
            "version": WORKER_RESULT_VERSION,
            "dispatch_id": item.get("dispatch_id"),
            "item_id": item_id,
            "repo": item.get("repo"),
            "issue_number": item.get("issue_number"),
            "status": status,
            "pr_number": pr_number,
            "error_summary": final_text if status != "completed" else None,
            "occurred_at": now_iso(),
            "source": "session_file_fallback",
            "evidence": {"trace_dir": str(trace_dir), "final_text": (final_text or "")[:500]},
        })
    return candidates


if __name__ == "__main__":
    init_db()
    print(json.dumps({"ok": True, "db": str(RESULTS_DB), "version": WORKER_RESULT_VERSION}, indent=2))
