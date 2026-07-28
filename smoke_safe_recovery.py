#!/usr/bin/env python3
"""Deterministic acceptance coverage for non-destructive queue recovery."""

import importlib.util
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import liveness
from worker_pools import WorkerPoolsManager


ROOT = Path(__file__).resolve().parent
NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)


def load_queue_module():
    spec = importlib.util.spec_from_file_location("cwc_queue_recovery_test", ROOT / "queue.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(root, *, progress=0.8):
    root.mkdir(parents=True, exist_ok=True)
    item_id = "coral-way-capital/orchestrator#21"
    item = {
        "id": item_id,
        "repo": "coral-way-capital/orchestrator",
        "issue_number": 21,
        "title": "Preserve this work",
        "started_at": (NOW - timedelta(hours=2)).isoformat(),
        "agent_log": str(root / "agent.log"),
        "phase": "final verification",
    }
    queue_path = root / "queue.json"
    queue_path.write_text(json.dumps({
        "pending": [],
        "in_progress": [item],
        "completed": [],
        "failed": [],
    }, indent=2), encoding="utf-8")
    worker = {
        "id": "worker-21",
        "item_id": item_id,
        "repo": item["repo"],
        "pid": 212121,
        "branch": "feat/agent-v2-21-safe-recovery",
        "worktree": "/worktrees/orchestrator/issue-21",
        "log_path": str(root / "agent.log"),
        "transcript_path": str(root / "transcript.jsonl"),
        "phase": "final verification",
        "progress": progress,
        "last_heartbeat_at": (NOW - timedelta(minutes=5)).isoformat(),
        "state": "active",
    }
    pool_path = root / "worker_pools.json"
    pool_path.write_text(json.dumps({"workers": [worker]}, indent=2), encoding="utf-8")
    return item_id, queue_path, pool_path


def configure(module, queue_path):
    module.QUEUE_FILE = queue_path
    module.SYNC_STATE_FILE = queue_path.parent / "sync-state.json"
    # Keep the compatibility ledger and event database out of this smoke test.
    def save_test_queue(queue):
        queue_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    module.save_queue = save_test_queue


def test_zombie_requeues_with_identity_and_recovery_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id, queue_path, pool_path = fixture(root)
        module = load_queue_module()
        configure(module, queue_path)
        manager = WorkerPoolsManager(str(pool_path))
        events = []
        traces = root / "traces"

        result = module.recover_item(
            item_id,
            requeue=True,
            pools_manager=manager,
            now=NOW,
            process_probe=lambda _pid: False,
            log_event_fn=lambda *args, **kwargs: events.append((args, kwargs)),
            traces_root=traces,
        )

        assert result["ok"] is True
        assert result["action"] == "requeued"
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
        assert [item["id"] for item in queue["pending"]] == [item_id]
        recovered = queue["pending"][0]
        assert recovered["recovery"]["branch"] == "feat/agent-v2-21-safe-recovery"
        assert recovered["recovery"]["worktree"] == "/worktrees/orchestrator/issue-21"
        assert recovered["recovery"]["phase"] == "final verification"
        assert manager.workers() == []
        manifest = json.loads(Path(result["recovery_manifest"]).read_text(encoding="utf-8"))
        assert manifest["queue_identity"]["id"] == item_id
        assert manifest["queue_identity"]["source_bucket"] == "in_progress"
        assert manifest["recovery_instructions"][-1] == (
            f"Re-dispatch {item_id} from pending; resume preserved work in "
            "/worktrees/orchestrator/issue-21 on branch feat/agent-v2-21-safe-recovery."
        )
        assert events[0][0] == ("issue.requeued",)


def test_dry_run_is_byte_for_byte_non_mutating():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id, queue_path, pool_path = fixture(root)
        module = load_queue_module()
        configure(module, queue_path)
        manager = WorkerPoolsManager(str(pool_path))
        before_queue = queue_path.read_bytes()
        before_pool = pool_path.read_bytes()
        events = []

        result = module.recover_item(
            item_id,
            requeue=True,
            dry_run=True,
            pools_manager=manager,
            now=NOW,
            process_probe=lambda _pid: False,
            log_event_fn=lambda *args, **kwargs: events.append((args, kwargs)),
            traces_root=root / "traces",
        )

        assert result["ok"] is True
        assert result["action"] == "would_requeue"
        assert queue_path.read_bytes() == before_queue
        assert pool_path.read_bytes() == before_pool
        assert not (root / "traces").exists()
        assert events == []


def test_recovery_is_idempotent_and_reloads_durable_pool_state():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        item_id, queue_path, pool_path = fixture(root)
        module = load_queue_module()
        configure(module, queue_path)
        manager = WorkerPoolsManager(str(pool_path))
        events = []
        kwargs = {
            "requeue": True,
            "pools_manager": manager,
            "now": NOW,
            "process_probe": lambda _pid: False,
            "log_event_fn": lambda *args, **kw: events.append((args, kw)),
            "traces_root": root / "traces",
        }

        first = module.recover_item(item_id, **kwargs)
        queue_after_first = queue_path.read_bytes()
        pool_after_first = pool_path.read_bytes()
        manifest_after_first = Path(first["recovery_manifest"]).read_bytes()
        second = module.recover_item(item_id, **kwargs)

        assert second["ok"] is True
        assert second["action"] == "already_pending"
        assert queue_path.read_bytes() == queue_after_first
        assert pool_path.read_bytes() == pool_after_first
        assert Path(first["recovery_manifest"]).read_bytes() == manifest_after_first
        assert len(events) == 1

        # A manager created before an external durable update must still see it.
        fresh_item_id, fresh_queue_path, fresh_pool_path = fixture(root / "replacement")
        fresh_module = load_queue_module()
        configure(fresh_module, fresh_queue_path)
        stale_manager = WorkerPoolsManager(str(fresh_pool_path))
        durable = json.loads(fresh_pool_path.read_text(encoding="utf-8"))
        durable["workers"][0]["pid"] = 999999
        fresh_pool_path.write_text(json.dumps(durable), encoding="utf-8")
        observed = []
        reload_result = fresh_module.recover_item(
            fresh_item_id,
            requeue=True,
            dry_run=True,
            pools_manager=stale_manager,
            now=NOW,
            process_probe=lambda pid: observed.append(pid) or False,
            log_event_fn=lambda *_args, **_kwargs: None,
            traces_root=root / "replacement" / "traces",
        )
        assert reload_result["action"] == "would_requeue"
        assert observed == [999999]


def test_live_near_complete_and_uncertain_workers_are_refused():
    for probe, expected_state in ((lambda _pid: True, liveness.STALE), (lambda _pid: None, liveness.STALE)):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item_id, queue_path, pool_path = fixture(root, progress=0.99)
            module = load_queue_module()
            configure(module, queue_path)
            before_queue = queue_path.read_bytes()
            before_pool = pool_path.read_bytes()

            result = module.recover_item(
                item_id,
                requeue=True,
                pools_manager=WorkerPoolsManager(str(pool_path)),
                now=NOW,
                process_probe=probe,
                log_event_fn=lambda *_args, **_kwargs: None,
                traces_root=root / "traces",
            )

            assert result["ok"] is False
            assert result["state"] == expected_state
            assert result["action"] == "refused"
            assert queue_path.read_bytes() == before_queue
            assert pool_path.read_bytes() == before_pool
            assert not (root / "traces").exists()


if __name__ == "__main__":
    test_zombie_requeues_with_identity_and_recovery_evidence()
    test_dry_run_is_byte_for_byte_non_mutating()
    test_recovery_is_idempotent_and_reloads_durable_pool_state()
    test_live_near_complete_and_uncertain_workers_are_refused()
    print("smoke_safe_recovery: ok")
