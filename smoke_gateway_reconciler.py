#!/usr/bin/env python3
"""Regression smoke tests for gateway final-response reconciliation."""
import json
import tempfile
from pathlib import Path

import gateway_reconciler


def test_parse_pr_and_failed_gateway_responses():
    pr_line = "2026-07-07 22:31:07 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:1783484324319: PR #1697"
    failed_line = "2026-07-07 23:25:52 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:1783487085313: FAILED: blocked by production command"
    assert gateway_reconciler.parse_gateway_response_line(pr_line)["pr_number"] == 1697
    failed = gateway_reconciler.parse_gateway_response_line(failed_line)
    assert failed["status"] == "failed"
    assert failed["error_summary"] == "FAILED: blocked by production command"


def test_parse_non_contract_final_response_for_repair():
    line = "2026-07-08 06:09:53 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:xyz: Ad-hoc verification passed — not suite green."
    parsed = gateway_reconciler.parse_gateway_response_line(line)
    assert parsed["dispatch_id"] == "webhook:cwc-issue-dispatch:xyz"
    assert parsed["status"] == "non_contract"
    assert parsed["error_summary"] == "Worker finished without contract output: Ad-hoc verification passed — not suite green."


def test_scan_gateway_log_keeps_latest_final_response_per_dispatch():
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "gateway.log"
        log.write_text("\n".join([
            "noise",
            "2026-07-07 22:31:07 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:abc: PR #10",
            "2026-07-07 22:31:37 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:abc: 💾 Self-improvement review: Patched SKILL.md",
            "2026-07-07 22:45:00 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:def: FAILED: tests failed",
        ]), encoding="utf-8")
        outcomes = gateway_reconciler.scan_gateway_log(log)
        assert outcomes["webhook:cwc-issue-dispatch:abc"]["pr_number"] == 10
        assert outcomes["webhook:cwc-issue-dispatch:def"]["status"] == "failed"
        assert "Self-improvement" not in outcomes["webhook:cwc-issue-dispatch:abc"]["final_response"]


def test_non_contract_linked_pr_trace_is_finalized_after_repair():
    """Review regression: trace metadata must reflect repaired completed outcome."""
    import queue as queue_mod

    original_base_dir = gateway_reconciler.BASE_DIR
    original_queue_file = queue_mod.QUEUE_FILE
    original_pool_cleanup = queue_mod._pool_cleanup
    original_check_linked_prs = queue_mod.check_linked_prs
    original_queue_log_event = queue_mod.log_event
    original_trace_writer = gateway_reconciler._update_agent_trace

    captured_trace_outcomes = []
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        gateway_reconciler.BASE_DIR = base
        queue_mod.QUEUE_FILE = base / "queue.json"
        queue_mod._pool_cleanup = lambda item_id: None
        queue_mod.check_linked_prs = lambda repo, issue_number: [{"pr_number": 456, "state": "OPEN"}]
        queue_mod.log_event = lambda *args, **kwargs: None
        gateway_reconciler._update_agent_trace = lambda item, outcome: captured_trace_outcomes.append(dict(outcome))

        queue_mod.QUEUE_FILE.write_text(json.dumps({
            "pending": [],
            "in_progress": [{
                "id": "coral-way-capital/demo#1",
                "repo": "coral-way-capital/demo",
                "issue_number": 1,
                "title": "Demo",
                "dispatch_id": "webhook:cwc-issue-dispatch:abc",
            }],
            "completed": [],
            "failed": [],
        }), encoding="utf-8")
        log = base / "gateway.log"
        log.write_text("2026-07-08 INFO gateway.platforms.webhook: [webhook] Response for webhook:cwc-issue-dispatch:abc: Done, see branch\n", encoding="utf-8")

        reconciled = gateway_reconciler.reconcile_in_progress(log)
        assert reconciled[0]["status"] == "completed"
        assert reconciled[0]["pr_number"] == 456

        q = json.loads(queue_mod.QUEUE_FILE.read_text(encoding="utf-8"))
        assert q["completed"][0]["pr_number"] == 456
        assert q["failed"] == []

        meta = json.loads((base / "traces" / "coral-way-capital_demo_1" / "meta.json").read_text(encoding="utf-8"))
        assert meta["reconciled_status"] == "completed"
        assert meta["reconciled_pr_number"] == 456
        assert captured_trace_outcomes[0]["status"] == "completed"
        assert captured_trace_outcomes[0]["pr_number"] == 456

    gateway_reconciler.BASE_DIR = original_base_dir
    queue_mod.QUEUE_FILE = original_queue_file
    queue_mod._pool_cleanup = original_pool_cleanup
    queue_mod.check_linked_prs = original_check_linked_prs
    queue_mod.log_event = original_queue_log_event
    gateway_reconciler._update_agent_trace = original_trace_writer


if __name__ == "__main__":
    test_parse_pr_and_failed_gateway_responses()
    test_parse_non_contract_final_response_for_repair()
    test_scan_gateway_log_keeps_latest_final_response_per_dispatch()
    test_non_contract_linked_pr_trace_is_finalized_after_repair()
    print("ok")
