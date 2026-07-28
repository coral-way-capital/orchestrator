#!/usr/bin/env python3
"""Deterministic worker-liveness acceptance fixture.

Uses fake clocks and process/session probes only. It never probes or signals a
real process and writes all state beneath a temporary directory.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import liveness
from dispatch_telemetry import normalize_dispatch_telemetry
from worker_pools import WorkerPoolsManager


BASE_TIME = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)


def worker(*, heartbeat_age, pid=424242, worker_id="worker-1", item_id="cwc/demo#16"):
    return {
        "id": worker_id,
        "item_id": item_id,
        "repo": "coral-way-capital/orchestrator",
        "pid": pid,
        "session_id": "session-16",
        "branch": "feat/agent-v2-16-worker-liveness",
        "worktree": "/worktrees/orchestrator/issue-16",
        "log_path": "/logs/worker.log",
        "transcript_path": "/logs/transcript.jsonl",
        "phase": "running focused tests",
        "progress": 0.75,
        "started_at": (BASE_TIME - timedelta(hours=3)).isoformat(),
        "last_heartbeat_at": (BASE_TIME - timedelta(seconds=heartbeat_age)).isoformat(),
        "state": "active",
    }


def write_pool(path, workers):
    path.write_text(json.dumps({"workers": workers}), encoding="utf-8")
    return WorkerPoolsManager(str(path))


def test_classification_uses_both_signals():
    # Evidence arrives within one minute: a failed PID probe alone is not dead.
    fresh = liveness.classify_worker(
        worker(heartbeat_age=59), now=BASE_TIME, process_probe=lambda _pid: False
    )
    assert fresh.state == liveness.LIVE
    assert fresh.stale is False

    # A missed heartbeat is stale, but a proven-live long process is never dead.
    long_job = liveness.classify_worker(
        worker(heartbeat_age=3 * 60 * 60),
        now=BASE_TIME,
        process_probe=lambda _pid: True,
    )
    assert long_job.state == liveness.STALE
    assert long_job.process_alive is True

    # Both stale heartbeat and failed process evidence are required for death.
    killed = liveness.classify_worker(
        worker(heartbeat_age=5 * 60 - 1),
        now=BASE_TIME,
        process_probe=lambda _pid: False,
    )
    assert killed.state == liveness.DEAD
    assert killed.stale is True

    # No PID/session evidence is observable as stale, never guessed dead.
    no_evidence = liveness.classify_worker(
        worker(heartbeat_age=5 * 60, pid=None),
        now=BASE_TIME,
        process_probe=lambda _pid: False,
    )
    assert no_evidence.state == liveness.STALE

    dead_session = liveness.classify_worker(
        worker(heartbeat_age=5 * 60, pid=None),
        now=BASE_TIME,
        session_probe=lambda _worker: False,
    )
    assert dead_session.state == liveness.DEAD


def test_uncertain_process_and_missing_heartbeat_are_never_dead():
    malformed_pid = liveness.classify_worker(
        worker(heartbeat_age=5 * 60, pid="not-a-pid"),
        now=BASE_TIME,
    )
    assert malformed_pid.state == liveness.STALE
    assert malformed_pid.process_alive is None

    no_heartbeat = worker(heartbeat_age=5 * 60)
    no_heartbeat.pop("last_heartbeat_at")
    missing = liveness.classify_worker(
        no_heartbeat,
        now=BASE_TIME,
        process_probe=lambda _pid: False,
    )
    assert missing.state == liveness.UNKNOWN
    assert missing.stale is False


def test_dispatch_preserves_available_process_and_recovery_metadata():
    telemetry = normalize_dispatch_telemetry({
        "process": {"pid": "424242"},
        "git": {"branch": "feat/agent-v2-16-worker-liveness"},
        "paths": {
            "worktree": "/worktrees/orchestrator/issue-16",
            "log": "/logs/worker.log",
        },
    })
    assert telemetry["pid"] == 424242
    assert telemetry["branch"] == "feat/agent-v2-16-worker-liveness"
    assert telemetry["worktree"] == "/worktrees/orchestrator/issue-16"
    assert telemetry["liveness_reliable"] is True


def test_heartbeat_records_phase_progress_with_fake_clock():
    with tempfile.TemporaryDirectory() as tmp:
        pool_path = Path(tmp) / "worker_pools.json"
        manager = write_pool(pool_path, [worker(heartbeat_age=59)])
        heartbeat_at = BASE_TIME + timedelta(seconds=1)

        assert liveness.record_heartbeat(
            manager,
            item_id="cwc/demo#16",
            phase="writing tests",
            progress=0.9,
            message="4/5 complete",
            now=heartbeat_at,
        )
        updated = manager.workers()[0]
        assert updated["last_heartbeat_at"] == heartbeat_at.isoformat()
        assert updated["phase"] == "writing tests"
        assert updated["progress"] == 0.9
        assert updated["message"] == "4/5 complete"


def test_heartbeat_authentication_and_validation():
    token = liveness.make_heartbeat_token("test-secret", "cwc/demo#16")
    assert liveness.verify_heartbeat_token(
        "test-secret", "cwc/demo#16", token
    )
    assert not liveness.verify_heartbeat_token(
        "test-secret", "cwc/demo#17", token
    )

    assert liveness.validate_heartbeat_payload({
        "item_id": "cwc/demo#16",
        "phase": "tests",
        "progress": 1.0,
    }) is None
    assert liveness.validate_heartbeat_payload({
        "item_id": "cwc/demo#16",
        "progress": 1.1,
    }) == "progress must be between 0 and 1"
    assert liveness.validate_heartbeat_payload({
        "item_id": "cwc/demo#16",
        "phase": {"not": "text"},
    }) == "phase must be a string"


def test_manager_probe_uses_the_same_two_signal_model():
    with tempfile.TemporaryDirectory() as tmp:
        pool_path = Path(tmp) / "worker_pools.json"
        manager = write_pool(pool_path, [worker(heartbeat_age=5 * 60 - 1)])
        results, matched = manager.liveness_probe(
            item_id="cwc/demo#16",
            now=BASE_TIME,
            process_probe=lambda _pid: False,
        )
        assert matched["id"] == "worker-1"
        assert results[0]["state"] == liveness.DEAD
        assert results[0]["stale"] is True


def test_reaping_is_safe_recoverable_audited_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pool_path = root / "worker_pools.json"
        live = worker(
            heartbeat_age=24 * 60 * 60,
            worker_id="worker-live",
            item_id="cwc/demo#live",
        )
        dead = worker(
            heartbeat_age=5 * 60 - 1,
            worker_id="worker-dead",
            item_id="cwc/demo#dead",
        )
        manager = write_pool(pool_path, [live, dead])
        events = []
        old_traces = os.environ.get("CWC_AGENT_TRACES_DIR")
        os.environ["CWC_AGENT_TRACES_DIR"] = str(root / "traces")
        try:
            probe = lambda pid: pid == live["pid"] and pid != dead["pid"]
            # Give fixtures distinct fake PIDs so the fake probe is unambiguous.
            live["pid"] = 111111
            dead["pid"] = 222222
            manager = write_pool(pool_path, [live, dead])

            first = liveness.reap_dead_workers(
                manager,
                now=BASE_TIME,
                process_probe=probe,
                log_event_fn=lambda *args, **kwargs: events.append((args, kwargs)),
            )
            assert first.count == 1
            assert first.errors == []
            assert [w["id"] for w in manager.workers()] == ["worker-live"]
            assert len(events) == 1
            assert events[0][0] == ("worker.reaped",)

            manifest_path = Path(first.reaped[0]["recovery_manifest"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for key in (
                "branch",
                "worktree",
                "log_path",
                "transcript_path",
                "phase",
                "progress",
                "recovery_instructions",
            ):
                assert manifest[key]

            second = liveness.reap_dead_workers(
                manager,
                now=BASE_TIME,
                process_probe=lambda _pid: True,
                log_event_fn=lambda *args, **kwargs: events.append((args, kwargs)),
            )
            assert second.count == 0
            assert len(events) == 1
        finally:
            if old_traces is None:
                os.environ.pop("CWC_AGENT_TRACES_DIR", None)
            else:
                os.environ["CWC_AGENT_TRACES_DIR"] = old_traces


def test_reaping_requires_a_durable_audit_event():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        pool_path = root / "worker_pools.json"
        dead = worker(heartbeat_age=5 * 60, pid=222222)
        manager = write_pool(pool_path, [dead])
        old_traces = os.environ.get("CWC_AGENT_TRACES_DIR")
        os.environ["CWC_AGENT_TRACES_DIR"] = str(root / "traces")
        try:
            def unavailable_audit(*_args, **_kwargs):
                raise OSError("event store unavailable")

            result = liveness.reap_dead_workers(
                manager,
                now=BASE_TIME,
                process_probe=lambda _pid: False,
                log_event_fn=unavailable_audit,
            )
            assert result.count == 0
            assert [w["id"] for w in manager.workers()] == [dead["id"]]
            assert result.errors == [{
                "worker_id": dead["id"],
                "error": "log: event store unavailable",
            }]
        finally:
            if old_traces is None:
                os.environ.pop("CWC_AGENT_TRACES_DIR", None)
            else:
                os.environ["CWC_AGENT_TRACES_DIR"] = old_traces


def main():
    test_classification_uses_both_signals()
    test_uncertain_process_and_missing_heartbeat_are_never_dead()
    test_dispatch_preserves_available_process_and_recovery_metadata()
    test_heartbeat_records_phase_progress_with_fake_clock()
    test_heartbeat_authentication_and_validation()
    test_manager_probe_uses_the_same_two_signal_model()
    test_reaping_is_safe_recoverable_audited_and_idempotent()
    test_reaping_requires_a_durable_audit_event()
    print("smoke_liveness: ok")


if __name__ == "__main__":
    main()
