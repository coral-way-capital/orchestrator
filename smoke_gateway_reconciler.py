#!/usr/bin/env python3
"""Regression smoke tests for gateway final-response reconciliation."""
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


if __name__ == "__main__":
    test_parse_pr_and_failed_gateway_responses()
    test_parse_non_contract_final_response_for_repair()
    test_scan_gateway_log_keeps_latest_final_response_per_dispatch()
    print("ok")
