#!/usr/bin/env python3
"""Deterministic integration and regression tests for structured worker results."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import atexit
from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

_TEST_HOME = tempfile.mkdtemp(prefix="orchestrator-worker-results-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import agent_traces
import backfill_report
import queue as queue_mod
import webhook_receiver
import worker_results


def _payload(**overrides):
    payload = {
        "version": 1,
        "dispatch_id": "webhook:cwc-issue-dispatch:test-15",
        "item_id": "coral-way-capital/demo#15",
        "status": "completed",
        "occurred_at": "2026-07-27T12:00:00+00:00",
        "pr_number": 115,
        "evidence": {"pr_url": "https://github.com/coral-way-capital/demo/pull/115"},
    }
    payload.update(overrides)
    return payload


def test_contract_requires_version_dispatch_and_timestamp():
    for field in ("version", "dispatch_id", "occurred_at"):
        payload = _payload()
        payload.pop(field)
        try:
            worker_results.validate_worker_result(payload)
        except worker_results.WorkerResultError:
            pass
        else:
            raise AssertionError(f"{field} must be required")


def test_contract_rejects_uncorrelatable_dispatch_and_naive_timestamp():
    for payload in (
        _payload(dispatch_id="webhook:cwc-issue-dispatch:"),
        _payload(occurred_at="2026-07-27T12:00:00"),
    ):
        try:
            worker_results.validate_worker_result(payload)
        except worker_results.WorkerResultError:
            pass
        else:
            raise AssertionError("uncorrelatable result must be rejected")


def test_receiver_auth_fails_closed_and_accepts_signed_payload():
    original_gateway = webhook_receiver.load_gateway_secret
    original_webhook = webhook_receiver.load_secret
    try:
        webhook_receiver.load_gateway_secret = lambda: ""
        webhook_receiver.load_secret = lambda: ""
        assert not webhook_receiver.verify_worker_result_auth("", b"{}", "")

        webhook_receiver.load_gateway_secret = lambda: "test-secret"
        body = json.dumps(_payload()).encode()
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        assert webhook_receiver.verify_worker_result_auth("", body, signature)
        assert not webhook_receiver.verify_worker_result_auth("", body + b" ", signature)
    finally:
        webhook_receiver.load_gateway_secret = original_gateway
        webhook_receiver.load_secret = original_webhook


def test_signed_http_result_updates_state_under_five_seconds_and_audits_duplicate():
    original_gateway = webhook_receiver.load_gateway_secret
    original_webhook = webhook_receiver.load_secret
    original_ingest = webhook_receiver.ingest_worker_result
    original_list = webhook_receiver.list_worker_results
    queue_state = {
        "pending": [],
        "in_progress": [{
            "id": _payload()["item_id"],
            "repo": "coral-way-capital/demo",
            "issue_number": 15,
            "dispatch_id": _payload()["dispatch_id"],
        }],
        "completed": [],
        "failed": [],
    }
    events = []

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "results.db"

        def ingest(payload, source):
            return worker_results.ingest_worker_result(
                payload,
                source=source,
                db_path=db_path,
                queue_loader=lambda: queue_state,
                queue_saver=lambda queue: None,
                event_logger=lambda event_type, **kwargs: events.append((event_type, kwargs)),
            )

        webhook_receiver.load_gateway_secret = lambda: "test-secret"
        webhook_receiver.load_secret = lambda: ""
        webhook_receiver.ingest_worker_result = ingest
        webhook_receiver.list_worker_results = lambda item_id=None: worker_results.list_results(
            item_id=item_id, db_path=db_path
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), webhook_receiver.IssueWebhookHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps(_payload()).encode()
            signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
            started = time.monotonic()
            for _ in range(2):
                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                conn.request(
                    "POST",
                    "/api/worker-result",
                    body=body,
                    headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
                )
                response = conn.getresponse()
                result = json.loads(response.read())
                assert response.status == 200, result
                conn.close()
            elapsed = time.monotonic() - started

            assert elapsed < 5.0, elapsed
            assert queue_state["completed"][0]["pr_number"] == 115
            assert queue_state["in_progress"] == []
            assert [event[0] for event in events] == [
                "issue.completed",
                "worker_result.received",
                "worker_result.duplicate",
            ]
            assert result["duplicate"] is True
            trace = __import__("agent_traces").get_latest_trace_for_item(_payload()["item_id"])
            assert trace["status"] == "completed"
            assert trace["pr_number"] == 115
            assert trace["exit_reason"] == "worker_result"

            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("GET", f"/api/worker-results?item_id={quote(_payload()['item_id'], safe='')}")
            response = conn.getresponse()
            listed = json.loads(response.read())
            conn.close()
            assert response.status == 200, listed
            assert len(listed["results"]) == 1

            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            oversized = b"x" * (webhook_receiver.MAX_WORKER_RESULT_BODY + 1)
            conn.request(
                "POST",
                "/api/worker-result",
                body=oversized,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            rejected = json.loads(response.read())
            conn.close()
            assert response.status == 413, rejected
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            webhook_receiver.load_gateway_secret = original_gateway
            webhook_receiver.load_secret = original_webhook
            webhook_receiver.ingest_worker_result = original_ingest
            webhook_receiver.list_worker_results = original_list


def test_session_fallback_never_infers_success_from_ambiguous_text():
    queue = {
        "in_progress": [{"id": _payload()["item_id"], "repo": "coral-way-capital/demo", "issue_number": 15}],
    }
    with tempfile.TemporaryDirectory() as td:
        trace = Path(td) / "coral-way-capital_demo_15"
        trace.mkdir()
        (trace / "final.txt").write_text("The change is not merged; review is pending.", encoding="utf-8")
        candidates = worker_results.scan_session_files(Path(td), queue=queue)
    assert candidates[0]["status"] == "unknown"
    report = backfill_report.build_report(queue, session_candidates=candidates)
    assert report["unknown"][0]["evidence_sources"] == ["session_file"]
    assert "review is pending" in report["unknown"][0]["evidence"]["final_text"]


def test_session_fallback_never_infers_success_from_pr_text_alone():
    queue = {
        "in_progress": [{"id": _payload()["item_id"], "repo": "coral-way-capital/demo", "issue_number": 15}],
    }
    with tempfile.TemporaryDirectory() as td:
        trace = Path(td) / "coral-way-capital_demo_15"
        trace.mkdir()
        (trace / "final.txt").write_text(
            "I found PR #115, but its checks are failing and the work is incomplete.",
            encoding="utf-8",
        )
        candidates = worker_results.scan_session_files(Path(td), queue=queue)
    assert candidates[0]["status"] == "unknown"


def test_stale_dispatch_result_cannot_finish_newer_dispatch():
    item = {
        "id": _payload()["item_id"],
        "dispatch_id": "webhook:cwc-issue-dispatch:newer",
    }
    queue = {"pending": [], "in_progress": [item], "completed": [], "failed": []}
    result = worker_results.validate_worker_result(_payload())
    summary = worker_results.apply_outcome_to_queue(result, queue)
    assert summary["dispatch_mismatch"] is True
    assert queue["in_progress"] == [item]
    assert queue["completed"] == []


def test_backfill_preserves_unknown_session_file_evidence():
    item_id = "coral-way-capital/demo#18"
    queue = {
        "in_progress": [{
            "id": item_id,
            "repo": "coral-way-capital/demo",
            "issue_number": 18,
            "dispatch_id": "webhook:cwc-issue-dispatch:ambiguous",
        }],
    }
    report = backfill_report.build_report(
        queue,
        session_candidates=[{
            "version": 1,
            "dispatch_id": "webhook:cwc-issue-dispatch:ambiguous",
            "item_id": item_id,
            "repo": "coral-way-capital/demo",
            "issue_number": 18,
            "status": "unknown",
            "error_summary": "The change is not merged; review is pending.",
            "occurred_at": "2026-07-27T12:00:00+00:00",
            "evidence": {"final_text": "The change is not merged; review is pending."},
        }],
    )
    assert report["summary"]["unknown"] == 1
    assert report["unknown"][0]["evidence_sources"] == ["session_file"]
    assert report["unknown"][0]["evidence"]["final_text"].startswith("The change")


def test_result_without_active_dispatch_correlation_cannot_finish_item():
    item = {"id": _payload()["item_id"]}
    queue = {"pending": [], "in_progress": [item], "completed": [], "failed": []}
    summary = worker_results.apply_outcome_to_queue(
        worker_results.validate_worker_result(_payload()), queue
    )
    assert summary["dispatch_unverifiable"] is True
    assert queue["in_progress"] == [item]
    assert queue["completed"] == []


def test_retry_repairs_queue_after_interrupted_first_delivery():
    persisted = {
        "pending": [],
        "in_progress": [{
            "id": _payload()["item_id"],
            "dispatch_id": _payload()["dispatch_id"],
        }],
        "completed": [],
        "failed": [],
    }
    fail_first_save = True

    def load():
        return deepcopy(persisted)

    def save(queue):
        nonlocal fail_first_save, persisted
        if fail_first_save:
            fail_first_save = False
            raise OSError("simulated interrupted queue save")
        persisted = deepcopy(queue)

    with tempfile.TemporaryDirectory() as td:
        kwargs = {
            "source": "worker_api",
            "db_path": Path(td) / "results.db",
            "queue_loader": load,
            "queue_saver": save,
            "event_logger": lambda *args, **kwargs: None,
        }
        try:
            worker_results.ingest_worker_result(_payload(), **kwargs)
        except OSError:
            pass
        else:
            raise AssertionError("first interrupted delivery must fail")
        result = worker_results.ingest_worker_result(_payload(), **kwargs)

    assert result["applied"] is True
    assert persisted["in_progress"] == []
    assert persisted["completed"][0]["pr_number"] == 115


def test_real_queue_and_shared_sqlite_ledger_update_under_five_seconds():
    payload = _payload(
        dispatch_id="webhook:cwc-issue-dispatch:shared-db",
        item_id="coral-way-capital/demo#99",
        issue_number=99,
        repo="coral-way-capital/demo",
    )
    queue_mod.save_queue({
        "pending": [],
        "in_progress": [{
            "id": payload["item_id"],
            "repo": payload["repo"],
            "issue_number": payload["issue_number"],
            "dispatch_id": payload["dispatch_id"],
        }],
        "completed": [],
        "failed": [],
    })
    started = time.monotonic()
    result = worker_results.ingest_worker_result(
        payload,
        source="worker_api",
        event_logger=lambda *args, **kwargs: None,
    )
    elapsed = time.monotonic() - started
    saved = queue_mod.load_queue()

    assert result["applied"] is True
    assert elapsed < 5.0, elapsed
    assert saved["completed"][0]["id"] == payload["item_id"]


def test_conflicting_duplicate_cannot_repair_or_mutate_queue():
    persisted = {
        "pending": [],
        "in_progress": [{
            "id": _payload()["item_id"],
            "dispatch_id": _payload()["dispatch_id"],
        }],
        "completed": [],
        "failed": [],
    }

    def load():
        return deepcopy(persisted)

    def save(queue):
        nonlocal persisted
        persisted = deepcopy(queue)

    with tempfile.TemporaryDirectory() as td:
        kwargs = {
            "source": "worker_api",
            "db_path": Path(td) / "results.db",
            "queue_loader": load,
            "queue_saver": save,
            "event_logger": lambda *args, **kwargs: None,
        }
        worker_results.ingest_worker_result(_payload(), **kwargs)
        persisted["completed"].clear()
        persisted["in_progress"] = [{
            "id": _payload()["item_id"],
            "dispatch_id": _payload()["dispatch_id"],
        }]
        result = worker_results.ingest_worker_result(
            _payload(pr_number=999), **kwargs
        )

    assert result["duplicate"] is True
    assert result["conflict"] is True
    assert result["applied"] is False
    assert persisted["in_progress"]
    assert persisted["completed"] == []


def test_concurrent_conflicting_results_have_one_winner():
    persisted = {
        "pending": [],
        "in_progress": [{
            "id": _payload()["item_id"],
            "dispatch_id": _payload()["dispatch_id"],
        }],
        "completed": [],
        "failed": [],
    }
    responses = []
    errors = []
    start = threading.Barrier(3)

    def load():
        return deepcopy(persisted)

    def save(queue):
        nonlocal persisted
        persisted = deepcopy(queue)

    with tempfile.TemporaryDirectory() as td:
        kwargs = {
            "source": "worker_api",
            "db_path": Path(td) / "results.db",
            "queue_loader": load,
            "queue_saver": save,
            "event_logger": lambda *args, **kwargs: None,
        }

        def submit(payload):
            try:
                start.wait()
                responses.append(worker_results.ingest_worker_result(payload, **kwargs))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=submit, args=(_payload(),)),
            threading.Thread(target=submit, args=(_payload(
                status="failed",
                pr_number=None,
                error_summary="deterministic failure",
            ),)),
        ]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=5)

    assert errors == []
    assert len(responses) == 2
    assert sum(bool(response["applied"]) for response in responses) == 1
    assert sum(bool(response["conflict"]) for response in responses) == 1
    assert len(persisted["completed"]) + len(persisted["failed"]) == 1
    winner = next(response for response in responses if response["applied"])
    expected_bucket = "completed" if winner["result"]["status"] == "completed" else "failed"
    assert len(persisted[expected_bucket]) == 1


def test_backfill_separates_resolved_and_unknown_with_evidence():
    resolved_item = {
        "id": "coral-way-capital/demo#15",
        "repo": "coral-way-capital/demo",
        "issue_number": 15,
        "dispatch_id": "webhook:cwc-issue-dispatch:resolved",
    }
    unknown_item = {
        "id": "coral-way-capital/demo#16",
        "repo": "coral-way-capital/demo",
        "issue_number": 16,
        "dispatch_id": "webhook:cwc-issue-dispatch:unknown",
    }
    queue = {"in_progress": [resolved_item, unknown_item]}
    report = backfill_report.build_report(
        queue,
        worker_results_rows=[
            {
                **_payload(),
                "item_id": resolved_item["id"],
                "dispatch_id": resolved_item["dispatch_id"],
            }
        ],
    )
    assert report["summary"] == {
        "total_in_progress": 2,
        "resolved": 1,
        "unknown": 1,
        "unknown_pct": 50.0,
    }
    assert report["resolved"][0]["evidence_sources"] == ["worker_result"]
    assert report["resolved"][0]["evidence"]["pr_url"].endswith("/115")
    assert report["unknown"][0]["evidence_sources"] == ["none"]
    assert report["unknown"][0]["evidence"] == {"reason": "no terminal evidence found"}
    assert report["unknown"][0]["resolved_status"] == "unknown"
    assert report["production_metric"] == {
        "target": "<5% unknown worker outcomes over a rolling seven-day window",
        "status": "pending_post_deploy_observation",
    }


def test_apply_report_handles_resolved_entry_without_dispatch_id():
    item_id = "coral-way-capital/demo#19"
    queue = {
        "pending": [],
        "in_progress": [{"id": item_id, "repo": "coral-way-capital/demo", "issue_number": 19}],
        "completed": [],
        "failed": [],
    }
    report = {
        "generated_at": "2026-07-27T12:00:00+00:00",
        "resolved": [{
            "item_id": item_id,
            "dispatch_id": None,
            "resolved_status": "completed",
            "pr_number": 119,
        }],
        "unknown": [],
    }
    moved = backfill_report.apply_report(report, queue)
    assert moved["completed"] == 1
    assert queue["completed"][0]["pr_number"] == 119


def test_structured_result_updates_agent_trace_metadata():
    item_id = "coral-way-capital/demo#20"
    dispatch_id = "webhook:cwc-issue-dispatch:trace"
    trace = agent_traces.upsert_trace(
        item_id=item_id,
        repo="coral-way-capital/demo",
        issue_number=20,
        dispatch_id=dispatch_id,
        status="dispatched",
        started_at="2026-07-27T11:59:00+00:00",
        model_provider="openai-codex",
        model="gpt-5.5",
        prompt_id="default",
    )
    queue_state = {
        "pending": [],
        "in_progress": [{
            "id": item_id,
            "repo": "coral-way-capital/demo",
            "issue_number": 20,
            "dispatch_id": dispatch_id,
        }],
        "completed": [],
        "failed": [],
    }
    events = []
    with tempfile.TemporaryDirectory() as td:
        worker_results.ingest_worker_result(
            _payload(item_id=item_id, issue_number=20, dispatch_id=dispatch_id, pr_number=120),
            db_path=Path(td) / "results.db",
            queue_loader=lambda: queue_state,
            queue_saver=lambda queue: None,
            event_logger=lambda event_type, **kwargs: events.append((event_type, kwargs)),
        )
    updated = agent_traces.get_latest_trace_for_item(item_id)
    assert updated["id"] == trace["id"]
    assert updated["status"] == "completed"
    assert updated["pr_number"] == 120
    assert updated["exit_reason"] == "worker_result"
    assert [event[0] for event in events] == ["issue.completed", "worker_result.received"]
    assert events[0][1]["details"]["pr_number"] == 120
    meta = json.loads(agent_traces.bundle_paths(item_id)["meta_json"].read_text(encoding="utf-8"))
    assert meta["worker_result_status"] == "completed"
    assert meta["worker_result_pr_number"] == 120
    assert agent_traces.bundle_paths(item_id)["final_txt"].read_text(encoding="utf-8") == "PR #120"


def test_backfill_uses_legacy_telemetry_dispatch_id_and_apply_preserves_unknown():
    item = {
        "id": "coral-way-capital/demo#17",
        "repo": "coral-way-capital/demo",
        "issue_number": 17,
        "telemetry": {"dispatch_id": "webhook:cwc-issue-dispatch:legacy"},
    }
    queue = {"pending": [], "in_progress": [item], "completed": [], "failed": []}
    report = backfill_report.build_report(
        queue,
        gateway_outcomes={
            "webhook:cwc-issue-dispatch:legacy": {
                "status": "failed",
                "error_summary": "FAILED: deterministic evidence",
                "final_response": "FAILED: deterministic evidence",
            }
        },
    )
    assert report["resolved"][0]["dispatch_id"] == "webhook:cwc-issue-dispatch:legacy"
    moved = backfill_report.apply_report(report, queue)
    assert moved["failed"] == 1
    assert queue["failed"][0]["error"] == "FAILED: deterministic evidence"

    no_dispatch_item = {
        "id": "coral-way-capital/demo#18",
        "repo": "coral-way-capital/demo",
        "issue_number": 18,
    }
    queue = {"pending": [], "in_progress": [no_dispatch_item], "completed": [], "failed": []}
    report = {
        "generated_at": "2026-07-27T12:00:00+00:00",
        "resolved": [{
            "item_id": no_dispatch_item["id"],
            "dispatch_id": None,
            "resolved_status": "completed",
            "pr_number": 118,
        }],
        "unknown": [],
    }
    moved = backfill_report.apply_report(report, queue)
    assert moved["completed"] == 1
    assert queue["completed"][0]["pr_number"] == 118


def test_backfill_does_not_resolve_new_dispatch_from_old_item_result():
    item = {
        "id": _payload()["item_id"],
        "dispatch_id": "webhook:cwc-issue-dispatch:new",
    }
    report = backfill_report.build_report(
        {"in_progress": [item]},
        worker_results_rows=[
            _payload(dispatch_id="webhook:cwc-issue-dispatch:old")
        ],
    )
    assert report["resolved"] == []
    assert report["unknown"][0]["resolved_status"] == "unknown"


if __name__ == "__main__":
    test_contract_requires_version_dispatch_and_timestamp()
    test_contract_rejects_uncorrelatable_dispatch_and_naive_timestamp()
    test_receiver_auth_fails_closed_and_accepts_signed_payload()
    test_signed_http_result_updates_state_under_five_seconds_and_audits_duplicate()
    test_session_fallback_never_infers_success_from_ambiguous_text()
    test_session_fallback_never_infers_success_from_pr_text_alone()
    test_stale_dispatch_result_cannot_finish_newer_dispatch()
    test_backfill_preserves_unknown_session_file_evidence()
    test_result_without_active_dispatch_correlation_cannot_finish_item()
    test_retry_repairs_queue_after_interrupted_first_delivery()
    test_real_queue_and_shared_sqlite_ledger_update_under_five_seconds()
    test_conflicting_duplicate_cannot_repair_or_mutate_queue()
    test_concurrent_conflicting_results_have_one_winner()
    test_backfill_separates_resolved_and_unknown_with_evidence()
    test_apply_report_handles_resolved_entry_without_dispatch_id()
    test_structured_result_updates_agent_trace_metadata()
    test_backfill_uses_legacy_telemetry_dispatch_id_and_apply_preserves_unknown()
    test_backfill_does_not_resolve_new_dispatch_from_old_item_result()
    print("ok")
