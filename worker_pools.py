#!/usr/bin/env python3
"""Worker pools manager for CWC issue queue dispatch.

Tracks active workers per repo and exposes helpers for assigning workers,
refreshing liveness, and reporting stable worker-shaped payloads.
"""
import json
import os
import time
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

    def _save(self, pools):
        pools["updated_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.worker_pools_file, "w") as f:
            json.dump(pools, f, indent=2)
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

    def register_worker(self, item_id, repo, started_at=None, pid=None, item_pid=None, session_id=None, dispatch_id=None, lease_seconds=2700, telemetry=None, log_path=None, transcript_path=None, status_url=None, status_path=None, telemetry_missing=None, liveness_reliable=None):
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
            "telemetry": telemetry or {},
            "telemetry_missing": telemetry_missing or [],
            "liveness_reliable": bool(liveness_reliable),
            "started_at": started,
            "claimed_at": datetime.now(timezone.utc).isoformat(),
            "lease_expires_at": lease_expires_at,
            "last_heartbeat_at": datetime.now(timezone.utc).isoformat(),
            "state": "active",
            "dispatches": 0,
            "last_status": "unknown",
        }
        pools = self._load()
        pools["workers"].append(worker)
        self._save(pools)
        return worker

    def refresh_worker(self, worker_id, pid=None, item_pid=None, started_at=None, state=None, last_status=None, dispatches=None, session_id=None, dispatch_id=None, heartbeat=True, telemetry=None, log_path=None, transcript_path=None, status_url=None, status_path=None, telemetry_missing=None, liveness_reliable=None):
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

    def remove_worker(self, worker_id):
        pools = self._load()
        workers = [w for w in pools.get("workers", []) if w.get("id") != worker_id]
        pools["workers"] = workers
        self._save(pools)
        return True

    def remove_worker_by_item(self, item_id):
        pools = self._load()
        workers = [w for w in pools.get("workers", []) if w.get("item_id") != item_id]
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

    def liveness_probe(self, worker_id=None, item_id=None, repo=None):
        pools = self._load()
        workers = pools.get("workers", [])
        if worker_id:
            workers = [w for w in workers if w.get("id") == worker_id]
        elif item_id:
            workers = [w for w in workers if w.get("item_id") == item_id]
        elif repo:
            workers = [w for w in workers if w.get("repo") == repo]

        now = datetime.now(timezone.utc)
        results = []
        worker = None
        for w in workers:
            pid = w.get("pid") or w.get("item_pid")
            is_running = False
            if pid:
                try:
                    rc = os.kill(int(pid), 0)
                    is_running = True
                except ProcessLookupError:
                    is_running = False
                except Exception:
                    is_running = False

            started_at = w.get("started_at") or w.get("claimed_at")
            state = w.get("state")
            if not is_running and started_at:
                try:
                    started = datetime.fromisoformat(started_at)
                    if (now - started).total_seconds() > 3600:
                        state = "stale"
                    elif not is_running:
                        state = "stale"
                except Exception:
                    if not is_running:
                        state = "stale"

            results.append({
                "id": w.get("id"),
                "item_id": w.get("item_id"),
                "repo": w.get("repo"),
                "pid": pid,
                "session_id": w.get("session_id"),
                "dispatch_id": w.get("dispatch_id"),
                "log_path": w.get("log_path"),
                "transcript_path": w.get("transcript_path"),
                "status_url": w.get("status_url"),
                "telemetry_missing": w.get("telemetry_missing", []),
                "liveness_reliable": bool(w.get("liveness_reliable")),
                "is_running": is_running,
                "state": state,
                "started_at": started_at,
                "last_status": w.get("last_status"),
            })
            worker = w

        return results, worker

    def reap_stale(self, stale_threshold=3600):
        pools = self._load()
        now = datetime.now(timezone.utc)
        stale = []
        for w in list(pools.get("workers", [])):
            pid = w.get("pid") or w.get("item_pid")
            is_running = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    is_running = True
                except Exception:
                    is_running = False

            started_at = w.get("started_at") or w.get("claimed_at")
            lease_expires_at = w.get("lease_expires_at")
            if not is_running and started_at:
                try:
                    if lease_expires_at and now > datetime.fromisoformat(lease_expires_at):
                        stale.append(w)
                        pools["workers"].remove(w)
                        continue
                    started = datetime.fromisoformat(started_at)
                    if (now - started).total_seconds() > stale_threshold:
                        stale.append(w)
                        pools["workers"].remove(w)
                except Exception:
                    pass

        if stale:
            self._save(pools)
        return stale

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
            "reaped_stale": [{"id": w.get("id"), "item_id": w.get("item_id")} for w in stale],
        }

    @staticmethod
    def _find_worker_in(pools, worker_id):
        for w in pools.get("workers", []):
            if w.get("id") == worker_id:
                return w
        return None
