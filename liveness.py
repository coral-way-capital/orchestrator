#!/usr/bin/env python3
"""Worker liveness classification and safe stale-worker reaping.

This module implements the heartbeat-driven liveness model required by
cwc-control-plane issue #16. It replaces the previous elapsed-time-only
reaping (which risked killing valid long-running work) with a two-signal
model:

  1. **Heartbeat staleness** – every active worker should emit a heartbeat
     (phase + progress) at least every ``HEARTBEAT_TIMEOUT_SECONDS`` (60 s
     by default).  A worker whose heartbeat is older than that is *stale*.
  2. **Process/session liveness** – a stale worker is only *dead* when an
     independent process probe (``kill -0 <pid>``) or session-path check
     confirms the worker process is gone.

Only workers that are **both** stale AND confirmed dead are eligible for
reaping.  A live process is never reaped by time alone.

Reaping is **idempotent** (re-reaping a worker that was already reaped is a
no-op) and **fully audited**: each reaped worker gets a JSON recovery
manifest written next to its trace bundle, and a structured event is logged
via :mod:`events`.

The module is stdlib-only and deterministic: it accepts injectable ``now``
and ``process_probe`` callables so tests can use fake clocks and fake
process probes with no sleeps.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: A worker is *stale* when its last heartbeat is older than this.
HEARTBEAT_TIMEOUT_SECONDS = 60

#: Stale workers whose process/session is confirmed dead are reaped.
#: A worker that has been stale this long without any heartbeat is
#: considered for reaping regardless of started_at (but still requires a
#: failed process check).
DEAD_CONFIRMATION_SECONDS = 5 * 60  # 5 minutes – killed-worker detection SLA

#: Worker lifecycle states.
LIVE = "live"
STALE = "stale"
DEAD = "dead"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def safe_item_id(item_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(item_id or "").strip())
    return safe.strip("._") or "unknown"


def default_process_probe(pid) -> bool:
    """Return ``True`` if *pid* is running, ``False`` otherwise.

    Uses ``os.kill(pid, 0)`` – no signal is sent, just an existence check.
    """
    if pid in (None, "", 0):
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, TypeError):
        return False
    except PermissionError:
        # Process exists but we lack permission – treat as running.
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@dataclass
class LivenessResult:
    """Outcome of classifying a single worker."""

    worker_id: str
    item_id: Optional[str]
    pid: Optional[int]
    state: str  # live | stale | dead | unknown
    heartbeat_age_seconds: Optional[float]
    stale: bool
    process_alive: Optional[bool]
    evidence_source: Optional[str] = None
    reason: str = ""
    recovery_manifest: Optional[str] = None


def classify_worker(
    worker: dict,
    *,
    now: Optional[datetime] = None,
    heartbeat_timeout: int = HEARTBEAT_TIMEOUT_SECONDS,
    process_probe: Callable[[object], bool] = default_process_probe,
    session_probe: Optional[Callable[[dict], Optional[bool]]] = None,
) -> LivenessResult:
    """Classify a single worker as live, stale, dead, or unknown.

    Decision matrix:

    ===========  ===========  ===========  ==================
    has PID      process      heartbeat    state
    ===========  ===========  ===========  ==================
    yes          alive        fresh        **live**
    yes          alive        stale        **stale** (not reapable)
    yes          dead         fresh        **live** (heartbeat evidence)
    yes          dead         stale        **dead**
    no           n/a          fresh        **live** (heartbeat-only)
    no           n/a          stale        **stale**
    ===========  ===========  ===========  ==================

    A live process is *never* marked stale/dead regardless of heartbeat age.
    """
    now = now or datetime.now(timezone.utc)

    wid = worker.get("id", "?")
    item_id = worker.get("item_id")
    pid = worker.get("pid") or worker.get("item_pid")

    # --- heartbeat age ---------------------------------------------------
    hb_value = worker.get("last_heartbeat_at") or worker.get("started_at") or worker.get("claimed_at")
    hb_dt = parse_iso(hb_value)
    hb_age: Optional[float] = None
    if hb_dt is not None:
        hb_age = (now - hb_dt).total_seconds()

    stale_by_heartbeat = (
        hb_age is not None and hb_age > heartbeat_timeout
    )

    # --- process probe ---------------------------------------------------
    process_alive: Optional[bool] = None
    evidence_source: Optional[str] = None
    if pid:
        evidence_source = "process"
        try:
            probe_result = process_probe(pid)
            process_alive = None if probe_result is None else bool(probe_result)
        except Exception:
            # A probe error is not evidence that the process is gone.
            process_alive = None
    elif session_probe is not None and (
        worker.get("session_id")
        or worker.get("session_path")
        or worker.get("status_path")
    ):
        evidence_source = "session"
        try:
            probe_result = session_probe(worker)
            process_alive = None if probe_result is None else bool(probe_result)
        except Exception:
            process_alive = None

    # --- decision --------------------------------------------------------
    if hb_age is None:
        return LivenessResult(
            worker_id=wid, item_id=item_id, pid=pid,
            state=UNKNOWN, heartbeat_age_seconds=None,
            stale=False, process_alive=process_alive,
            evidence_source=evidence_source,
            reason="no heartbeat timestamp",
        )

    # A fresh heartbeat is direct evidence of recent worker activity. A failed
    # PID/session probe alone must never make it dead.
    if not stale_by_heartbeat:
        return LivenessResult(
            worker_id=wid, item_id=item_id, pid=pid,
            state=LIVE, heartbeat_age_seconds=hb_age,
            stale=False, process_alive=process_alive,
            evidence_source=evidence_source,
            reason=f"heartbeat fresh ({int(hb_age)}s old)",
        )

    # A stale heartbeat plus independently failed process/session evidence is
    # the only route to DEAD.
    if process_alive is False:
        label = f"pid {pid}" if evidence_source == "process" else "session"
        return LivenessResult(
            worker_id=wid, item_id=item_id, pid=pid,
            state=DEAD, heartbeat_age_seconds=hb_age,
            stale=True, process_alive=False,
            evidence_source=evidence_source,
            reason=(
                f"heartbeat {int(hb_age)}s > {heartbeat_timeout}s; "
                f"{label} not running"
            ),
        )

    evidence = (
        f"{evidence_source} alive"
        if process_alive is True
        else "no conclusive process/session evidence"
    )
    return LivenessResult(
        worker_id=wid, item_id=item_id, pid=pid,
        state=STALE, heartbeat_age_seconds=hb_age,
        stale=True, process_alive=process_alive,
        evidence_source=evidence_source,
        reason=f"heartbeat {int(hb_age)}s > {heartbeat_timeout}s; {evidence}",
    )


def classify_workers(
    workers: list,
    *,
    now: Optional[datetime] = None,
    heartbeat_timeout: int = HEARTBEAT_TIMEOUT_SECONDS,
    process_probe: Callable[[object], bool] = default_process_probe,
    session_probe: Optional[Callable[[dict], Optional[bool]]] = None,
) -> list:
    """Classify a list of workers, returning :class:`LivenessResult` list."""
    now = now or datetime.now(timezone.utc)
    return [
        classify_worker(
            w, now=now, heartbeat_timeout=heartbeat_timeout,
            process_probe=process_probe, session_probe=session_probe,
        )
        for w in workers
    ]


# ---------------------------------------------------------------------------
# Recovery manifest
# ---------------------------------------------------------------------------

RECOVERY_MANIFEST_NAME = "reaper_recovery.json"


def _traces_root() -> Path:
    return Path(
        os.environ.get(
            "CWC_AGENT_TRACES_DIR",
            Path(os.environ.get("CWC_ISSUE_QUEUE_DIR", Path.home() / ".hermes" / "issue-queue"))
            / "traces",
        )
    )


def trace_dir_for_item(item_id: str) -> Path:
    return _traces_root() / safe_item_id(item_id)


def write_recovery_manifest(worker: dict, result: LivenessResult, *, now: Optional[datetime] = None) -> Path:
    """Write a JSON recovery manifest for a reaped worker.

    Preserves branch, worktree, logs and recovery instructions so an operator
    can resume the work manually. The manifest is written next to the trace
    bundle (``<traces>/<item>/reaper_recovery.json``) so it survives reaping.
    """
    now = now or datetime.now(timezone.utc)
    item_id = worker.get("item_id") or "unknown"
    tdir = trace_dir_for_item(item_id)
    tdir.mkdir(parents=True, exist_ok=True)
    manifest_path = tdir / RECOVERY_MANIFEST_NAME

    manifest = {
        "reaped_at": now.isoformat(),
        "worker_id": worker.get("id"),
        "item_id": item_id,
        "repo": worker.get("repo"),
        "pid": result.pid,
        "dispatch_id": worker.get("dispatch_id"),
        "session_id": worker.get("session_id"),
        "started_at": worker.get("started_at"),
        "last_heartbeat_at": worker.get("last_heartbeat_at"),
        "liveness_state": result.state,
        "liveness_reason": result.reason,
        "heartbeat_age_seconds": result.heartbeat_age_seconds,
        "evidence_source": result.evidence_source,
        # Preserve pointers so the work is recoverable.
        "branch": worker.get("branch"),
        "worktree": worker.get("worktree") or worker.get("local_path"),
        "log_path": worker.get("log_path"),
        "transcript_path": worker.get("transcript_path"),
        "logs": [
            path for path in (
                worker.get("log_path"),
                worker.get("transcript_path"),
            )
            if path
        ],
        "phase": worker.get("phase"),
        "progress": worker.get("progress"),
        "message": worker.get("message"),
        "trace_dir": str(tdir),
        "recovery_instructions": [
            f"Inspect logs: {worker.get('log_path', '<unknown>')}",
            f"Resume in worktree: {worker.get('worktree') or worker.get('local_path', '<unknown>')}",
            f"Trace bundle: {tdir}",
            "Re-dispatch the issue from the Mission Control dashboard if the work was incomplete.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------

@dataclass
class ReapResult:
    reaped: list = field(default_factory=list)
    skipped_live: list = field(default_factory=list)
    skipped_already_reaped: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.reaped)


def reap_dead_workers(
    pools_manager,
    *,
    now: Optional[datetime] = None,
    heartbeat_timeout: int = HEARTBEAT_TIMEOUT_SECONDS,
    process_probe: Callable[[object], bool] = default_process_probe,
    session_probe: Optional[Callable[[dict], Optional[bool]]] = None,
    log_event_fn: Optional[Callable] = None,
) -> ReapResult:
    """Reap workers that are **both stale and confirmed dead**.

    This function is **idempotent**: once a worker is removed from the pool,
    a second call will not produce a duplicate reap or duplicate audit events.

    Guarantees (issue #16 acceptance):
      * A live process is never reaped, no matter how stale the heartbeat.
      * A dead process is detected (eligible for reap) within
        ``DEAD_CONFIRMATION_SECONDS`` of its last heartbeat.
      * Every reaped worker gets a recovery manifest and a structured event.

    :param pools_manager: :class:`worker_pools.WorkerPoolsManager`.
    :param now: injectable clock for deterministic tests.
    :param heartbeat_timeout: seconds before a worker is considered stale.
    :param process_probe: injectable process-liveness check for tests.
    :param log_event_fn: structured event logger; defaults to ``events.log_event``.
    """
    now = now or datetime.now(timezone.utc)

    # Resolve event logger lazily so the module is importable without events.py.
    if log_event_fn is None:
        try:
            import events as _events
            log_event_fn = _events.log_event
        except Exception:
            log_event_fn = lambda *a, **kw: None  # noqa: E731

    result = ReapResult()

    # Snapshot current workers to avoid mutation-during-iteration and to
    # make idempotency guarantees explicit.
    current_workers = list(pools_manager.workers())
    if not current_workers:
        return result

    for worker in current_workers:
        wid = worker.get("id", "?")
        item_id = worker.get("item_id")

        classification = classify_worker(
            worker, now=now,
            heartbeat_timeout=heartbeat_timeout,
            process_probe=process_probe,
            session_probe=session_probe,
        )

        if classification.state == LIVE:
            result.skipped_live.append({
                "worker_id": wid, "item_id": item_id,
                "reason": classification.reason,
            })
            continue

        if classification.state != DEAD or not classification.stale:
            # STALE or UNKNOWN – not dead yet; leave alone.
            result.skipped_live.append({
                "worker_id": wid, "item_id": item_id,
                "reason": f"{classification.state}: {classification.reason}",
            })
            continue

        # --- Reap this dead worker ---------------------------------------
        # Write recovery manifest BEFORE removing (so item_id path is stable).
        try:
            manifest_path = write_recovery_manifest(worker, classification, now=now)
            classification.recovery_manifest = str(manifest_path)
        except Exception as exc:
            result.errors.append({"worker_id": wid, "error": f"manifest: {exc}"})
            # Preservation is a prerequisite for reaping.
            continue

        # Remove the exact worker only after its recovery record is durable.
        try:
            removed = pools_manager.remove_worker(wid)
            if not removed:
                result.skipped_already_reaped.append({
                    "worker_id": wid,
                    "item_id": item_id,
                })
                continue
        except Exception as exc:
            result.errors.append({"worker_id": wid, "error": f"remove: {exc}"})
            continue

        reaped_record = {
            "worker_id": wid,
            "item_id": item_id,
            "repo": worker.get("repo"),
            "pid": classification.pid,
            "state": classification.state,
            "reason": classification.reason,
            "heartbeat_age_seconds": classification.heartbeat_age_seconds,
            "evidence_source": classification.evidence_source,
            "phase": worker.get("phase"),
            "progress": worker.get("progress"),
            "recovery_manifest": classification.recovery_manifest,
            "reaped_at": now.isoformat(),
        }
        result.reaped.append(reaped_record)

        # Audit event (idempotent: we only emit for workers that were present).
        try:
            log_event_fn(
                "worker.reaped",
                item_id=item_id,
                repo=worker.get("repo"),
                details=reaped_record,
            )
        except Exception as exc:
            result.errors.append({"worker_id": wid, "error": f"log: {exc}"})

    return result


# ---------------------------------------------------------------------------
# Heartbeat recording
# ---------------------------------------------------------------------------

def record_heartbeat(
    pools_manager,
    worker_id: Optional[str] = None,
    *,
    item_id: Optional[str] = None,
    phase: Optional[str] = None,
    progress: Optional[float] = None,
    message: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Record a heartbeat for a worker, updating phase/progress.

    Looks up the worker by ``worker_id`` or ``item_id``, stamps
    ``last_heartbeat_at`` to *now*, and stores ``phase``/``progress``/
    ``message`` on the worker record so the dashboard and reaper can
    distinguish live long jobs from dead ones.

    Returns ``True`` if a worker was updated, ``False`` otherwise.
    """
    now_dt = now or datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()

    workers = pools_manager.workers()
    target = None
    for w in workers:
        if worker_id and w.get("id") == worker_id:
            target = w
            break
        if item_id and w.get("item_id") == item_id:
            target = w
            break

    if not target:
        return False

    updates = {}
    if phase is not None:
        updates["phase"] = phase
    if progress is not None:
        try:
            updates["progress"] = float(progress)
        except (TypeError, ValueError):
            pass
    if message is not None:
        updates["message"] = message

    pools = pools_manager._load()
    for worker in pools.get("workers", []):
        if worker.get("id") == target.get("id"):
            worker.update(updates)
            worker["last_heartbeat_at"] = now_iso
            pools_manager._save(pools)
            return True
    return False


# ---------------------------------------------------------------------------
# CLI / smoke
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(json.dumps({
        "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS,
        "dead_confirmation_seconds": DEAD_CONFIRMATION_SECONDS,
        "states": [LIVE, STALE, DEAD, UNKNOWN],
    }, indent=2))
