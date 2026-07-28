#!/usr/bin/env python3
"""Worker pools manager for CWC issue queue dispatch.

Tracks active workers per repo and exposes helpers for assigning workers,
refreshing liveness, and reporting stable worker-shaped payloads.
"""
import json
import os
import tempfile
import time
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone

BASE_DIR = os.path.expanduser("~/.hermes/issue-queue")
WORKER_POOLS_FILE = os.path.join(BASE_DIR, "worker_pools.json")

MAX_WORKERS = 2
MAX_WORKERS_PER_REPO = 1


class WorkerPoolsManager:
    def __init__(self, worker_pools_file):
        self.worker_pools_file = worker_pools_file
        self._pools = self._load()

    def _load(self):
        try:
            with open(self.worker_pools_file) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "max_workers": MAX_WORKERS,
                "max_workers_per_repo": MAX_WORKERS_PER_REPO,
                "workers": [],
            }

    @contextmanager
    def _exclusive_lock(self):
        """Serialize durable pool read/modify/write operations."""
        parent = os.path.dirname(self.worker_pools_file) or "."
        os.makedirs(parent, exist_ok=True)
        lock_path = self.worker_pools_file + ".lock"
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _save(self, pools):
        pools["updated_at"] = datetime.now(timezone.utc).isoformat()
        parent = os.path.dirname(self.worker_pools_file) or "."
        os.makedirs(parent, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".worker-pools-", dir=parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(pools, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, self.worker_pools_file)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self._pools = pools

    def _find_worker(self, worker_id):
        for worker in self._pools.get("workers", []):
            if worker.get("id") == worker_id:
                return worker
        return None

    def _find_worker_by_item(self, item_id):
        for worker in self._pools.get("workers", []):
            if worker.get("item_id") == item_id:
                return worker
        return None

    def _find_worker_by_repo(self, repo):
        for worker in self._pools.get("workers", []):
            if worker.get("repo") == repo:
                return worker
        return None

    def _next_worker_id(self):
        existing = [int(w.get("id", "worker-0").split("-")[-1]) for w in self._pools.get("workers", []) if w.get("id", "").startswith("worker-")]
        return f"worker-{(max(existing) + 1) if existing else 1}"

    def register_worker(self, item_id, repo, started_at=None, pid=None, item_pid=None, session_id=None, dispatch_id=None, lease_seconds=2700, telemetry=None, log_path=None, transcript_path=None, status_url=None, status_path=None, session_path=None, branch=None, worktree=None, telemetry_missing=None, liveness_reliable=None):
        # Reload from disk to avoid stale in-memory state when external
        # processes (queue.py CLI, manual resets) have modified the pool file.
        self._pools = self._load()
        existing = self._find_worker_by_item(item_id)
        if existing:
            return existing

        existing_by_repo = self._find_worker_by_repo(repo)
        if existing_by_repo:
            return existing_by_repo

        if self.active_worker_count() >= MAX_WORKERS:
            return None

        started = started_at or datetime.now(timezone.utc).isoformat()
        try:
            from datetime import timedelta
            lease_expires_at = (datetime.fromisoformat(started) + timedelta(seconds=lease_seconds)).isoformat()
        except Exception:
            lease_expires_at = None
        worker = {
            "id": self._next_worker_id(),
            "repo": repo,
            "item_id": item_id,
            "pid": pid,
            "item_pid": item_pid,
            "session_id": session_id,
            "dispatch_id": dispatch_id,
            "log_path": log_path,
            "transcript_path": transcript_path,
            "status_url": status_url,
            "status_path": status_path,
            "session_path": session_path,
            "branch": branch,
            "worktree": worktree,
            "telemetry": telemetry or {},
            "telemetry_missing": telemetry_missing or [],
            "liveness_reliable": bool(liveness_reliable),
            "started_at": started,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "lease_expires_at": lease_expires_at,
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "phase": None,
            "progress": None,
            "state": "active",
            "dispatches": 0,
            "last_status": "unknown",
        }
        pools = self._load()
        pools["workers"].append(worker)
        self._save(pools)
        return worker

    def refresh_worker(self, worker_id, pid=None, item_pid=None, started_at=None, state=None, last_status=None, dispatches=None, session_id=None, dispatch_id=None, heartbeat=True, telemetry=None, log_path=None, transcript_path=None, status_url=None, status_path=None, session_path=None, branch=None, worktree=None, telemetry_missing=None, liveness_reliable=None):
        pools = self._load()
        worker = self._find_worker_in(pools, worker_id)
        if not worker:
            return False
        if pid is not None:
            worker["pid"] = pid
        if item_pid is not None:
            worker["item_pid"] = item_pid
        if started_at is not None:
            worker["started_at"] = started_at
        if state is not None:
            worker["state"] = state
        if last_status is not None:
            worker["last_status"] = last_status
        if dispatches is not None:
            worker["dispatches"] = dispatches
        if session_id is not None:
            worker["session_id"] = session_id
        if dispatch_id is not None:
            worker["dispatch_id"] = dispatch_id
        if log_path is not None:
            worker["log_path"] = log_path
        if transcript_path is not None:
            worker["transcript_path"] = transcript_path
        if status_url is not None:
            worker["status_url"] = status_url
        if status_path is not None:
            worker["status_path"] = status_path
        if session_path is not None:
            worker["session_path"] = session_path
        if branch is not None:
            worker["branch"] = branch
        if worktree is not None:
            worker["worktree"] = worktree
        if telemetry is not None:
            worker["telemetry"] = telemetry
        if telemetry_missing is not None:
            worker["telemetry_missing"] = telemetry_missing
        if liveness_reliable is not None:
            worker["liveness_reliable"] = bool(liveness_reliable)
        if heartbeat:
            worker["last_heartbeat_at"] = datetime.now(timezone.utc).isoformat()
        self._save(pools)
        return True

    def record_heartbeat(self, worker_id=None, *, item_id=None, phase=None, progress=None, message=None):
        """Record a heartbeat with optional phase/progress for a worker.

        Delegates to :func:`liveness.record_heartbeat`. The heartbeat
        stamps ``last_heartbeat_at`` to now and stores ``phase``/``progress``
        so the reaper can distinguish live long jobs from dead workers.
        """
        try:
            import liveness as _liveness
            return _liveness.record_heartbeat(
                self, worker_id=worker_id, item_id=item_id,
                phase=phase, progress=progress, message=message,
            )
        except Exception:
            return False

    def classify_liveness(self):
        """Return liveness classification for all workers.

        Delegates to :func:`liveness.classify_workers`.
        """
        try:
            import liveness as _liveness
            return _liveness.classify_workers(self.workers())
        except Exception:
            return []

    def remove_worker(self, worker_id):
        with self._exclusive_lock():
            pools = self._load()
            previous = pools.get("workers", [])
            workers = [w for w in previous if w.get("id") != worker_id]
            if len(workers) == len(previous):
                return False
            pools["workers"] = workers
            self._save(pools)
            return True

    def remove_worker_if_unchanged(self, expected_worker):
        """Remove only the exact durable worker that was classified.

        A heartbeat or external refresh between classification and recovery
        changes the record and therefore fails closed instead of deleting a
        newly-live worker.
        """
        with self._exclusive_lock():
            pools = self._load()
            worker_id = expected_worker.get("id")
            current = self._find_worker_in(pools, worker_id)
            if current != expected_worker:
                return False
            pools["workers"].remove(current)
            self._save(pools)
            return True

    def remove_worker_by_item(self, item_id):
        with self._exclusive_lock():
            pools = self._load()
            previous = pools.get("workers", [])
            workers = [w for w in previous if w.get("item_id") != item_id]
            if len(workers) == len(previous):
                return False
            pools["workers"] = workers
            self._save(pools)
            return True

    def active_worker_count(self):
        pools = self._load()
        return sum(1 for w in pools.get("workers", []) if w.get("state") == "active")

    def available_slots(self):
        return max(0, MAX_WORKERS - self.active_worker_count())

    def active_workers(self):
        pools = self._load()
        return [w for w in pools.get("workers", []) if w.get("state") == "active"]

    def workers(self):
        return list(self._load().get("workers", []))

    def pool_for_repo(self, repo):
        pools = self._load()
        for w in pools.get("workers", []):
            if w.get("repo") == repo:
                return w
        return None

    def liveness_probe(
        self,
        worker_id=None,
        item_id=None,
        repo=None,
        *,
        now=None,
        process_probe=None,
        session_probe=None,
    ):
        """Return worker diagnostics using the canonical two-signal model."""
        import liveness as _liveness

        pools = self._load()
        workers = pools.get("workers", [])
        if worker_id:
            workers = [w for w in workers if w.get("id") == worker_id]
        elif item_id:
            workers = [w for w in workers if w.get("item_id") == item_id]
        elif repo:
            workers = [w for w in workers if w.get("repo") == repo]

        results = []
        worker = None
        for w in workers:
            classification = _liveness.classify_worker(
                w,
                now=now,
                process_probe=process_probe or _liveness.default_process_probe,
                session_probe=session_probe,
            )

            results.append({
                "id": w.get("id"),
                "item_id": w.get("item_id"),
                "repo": w.get("repo"),
                "pid": classification.pid,
                "session_id": w.get("session_id"),
                "dispatch_id": w.get("dispatch_id"),
                "log_path": w.get("log_path"),
                "transcript_path": w.get("transcript_path"),
                "status_url": w.get("status_url"),
                "telemetry_missing": w.get("telemetry_missing", []),
                "liveness_reliable": bool(w.get("liveness_reliable")),
                "is_running": classification.process_alive,
                "state": classification.state,
                "stale": classification.stale,
                "heartbeat_age_seconds": classification.heartbeat_age_seconds,
                "reason": classification.reason,
                "started_at": w.get("started_at") or w.get("claimed_at"),
                "last_status": w.get("last_status"),
            })
            worker = w

        return results, worker

    def reap_stale(self, stale_threshold=3600):
        """Reap workers that are stale AND confirmed dead.

        .. deprecated-logic::
            Previous versions reaped based on elapsed time alone, which
            violated issue #16 ("Live long jobs are never reaped by time
            alone"). This now delegates to :func:`liveness.reap_dead_workers`
            which requires BOTH a stale heartbeat AND a failed
            process/session check before reaping. A live process is never
            reaped regardless of how much time has elapsed.

        ``stale_threshold`` is accepted for backward compatibility but no
            longer causes reaping by itself — it only feeds the heartbeat
            staleness comparison.
        """
        try:
            import liveness as _liveness
            # Use the heartbeat timeout if caller passed the legacy
            # stale_threshold; otherwise default to the 60 s heartbeat SLA.
            heartbeat_timeout = (
                stale_threshold if stale_threshold != 3600
                else _liveness.HEARTBEAT_TIMEOUT_SECONDS
            )
            result = _liveness.reap_dead_workers(
                self,
                heartbeat_timeout=heartbeat_timeout,
            )
            return result.reaped
        except Exception:
            # Never let reaping crash the caller (health endpoint, cron).
            return []

    def health(self):
        stale = self.reap_stale()
        pools = self._load()
        return {
            "ok": True,
            "max_workers": MAX_WORKERS,
            "max_workers_per_repo": MAX_WORKERS_PER_REPO,
            "active_workers": self.active_worker_count(),
            "available_slots": self.available_slots(),
            "workers": self.workers(),
            "reaped_stale": [
                {"id": w.get("worker_id"), "item_id": w.get("item_id")}
                for w in stale
            ],
        }

    @staticmethod
    def _find_worker_in(pools, worker_id):
        for w in pools.get("workers", []):
            if w.get("id") == worker_id:
                return w
        return None
