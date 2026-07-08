#!/usr/bin/env python3
"""Smoke test for agent_traces using only temporary files."""

import json
import os
import tempfile


def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CWC_ISSUE_QUEUE_DIR"] = tmp

        import agent_traces

        item_id = "coral-way-capital/audit-agent#42"
        paths = agent_traces.ensure_trace_bundle(item_id)
        paths["meta_json"].write_text(json.dumps({"session_id": "sess-smoke"}), encoding="utf-8")
        paths["prompt_md"].write_text("Implement the issue.\n", encoding="utf-8")
        paths["transcript_jsonl"].write_text('{"role":"assistant","content":"done"}\n', encoding="utf-8")
        paths["tool_calls_jsonl"].write_text('{"tool":"shell","status":"ok"}\n', encoding="utf-8")
        paths["stdout_log"].write_text("line 1\nline 2\n", encoding="utf-8")
        paths["final_txt"].write_text("Created PR #123\n", encoding="utf-8")

        trace = agent_traces.upsert_trace(
            id="trace-smoke",
            item_id=item_id,
            repo="coral-way-capital/audit-agent",
            issue_number=42,
            session_id="sess-smoke",
            dispatch_id="dispatch-smoke",
            pid=12345,
            status="completed",
            model_provider="openai-codex",
            model="gpt-5.5",
            prompt_id="default",
            pr_number=123,
            exit_reason="completed",
            log_path=str(paths["stdout_log"]),
            transcript_path=str(paths["transcript_jsonl"]),
            trace_dir=str(paths["trace_dir"]),
        )
        payload = agent_traces.get_agent_trace_payload(item_id)

        assert trace["id"] == "trace-smoke"
        assert payload["has_trace"] is True
        assert payload["has_bundle"] is True
        assert payload["trace"]["session_id"] == "sess-smoke"
        assert payload["bundle"]["files"]["stdout.log"]["tail"].endswith("line 2\n")
        assert payload["bundle"]["files"]["meta.json"]["json"]["session_id"] == "sess-smoke"

        print(json.dumps({
            "ok": True,
            "db": str(agent_traces.db_path()),
            "trace_dir": payload["bundle"]["trace_dir"],
            "files": sorted(payload["bundle"]["files"].keys()),
        }, indent=2))


if __name__ == "__main__":
    main()
