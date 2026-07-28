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
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

_TEST_HOME = tempfile.mkdtemp(prefix="orchestrator-worker-results-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import agent_traces
import backfill_report
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
                "worker_result.received",
                "worker_result.duplicate",
            ]
            assert result["duplicate"] is True

            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            conn.request("GET", f"/api/worker-results?item_id={quote(_payload()['item_id'], safe='')}")
            response = conn.getresponse()
            listed = json.loads(response.read())
            conn.close()
            assert response.status == 200, listed
            assert len(listed["results"]) == 1
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


if __name__ == "__main__":
    test_contract_requires_version_dispatch_and_timestamp()
    test_receiver_auth_fails_closed_and_accepts_signed_payload()
    test_signed_http_result_updates_state_under_five_seconds_and_audits_duplicate()
    test_session_fallback_never_infers_success_from_ambiguous_text()
    test_stale_dispatch_result_cannot_finish_newer_dispatch()
    test_backfill_preserves_unknown_session_file_evidence()
    test_backfill_separates_resolved_and_unknown_with_evidence()
    test_apply_report_handles_resolved_entry_without_dispatch_id()
    test_structured_result_updates_agent_trace_metadata()
    test_backfill_uses_legacy_telemetry_dispatch_id_and_apply_preserves_unknown()
    print("ok")
