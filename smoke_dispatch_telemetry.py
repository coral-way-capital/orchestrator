#!/usr/bin/env python3
"""Deterministic telemetry fixtures for one dispatch and unsupported providers."""
from __future__ import annotations

import tempfile
from pathlib import Path

import dispatch_telemetry
import worker_results


DISPATCH_ID = "webhook:cwc-issue-dispatch:telemetry-exact"


def _exact_terminal():
    return {
        "version": 1,
        "dispatch_id": DISPATCH_ID,
        "item_id": "coral-way-capital/demo#20",
        "repo": "coral-way-capital/demo",
        "issue_number": 20,
        "status": "completed",
        "occurred_at": "2026-07-27T12:02:03.456000+00:00",
        "pr_number": 220,
        "telemetry": {
            "usage": {
                "status": "available",
                "input_tokens": 1200,
                "output_tokens": 345,
                "cached_input_tokens": 800,
                "reasoning_tokens": 90,
                "total_tokens": 1545,
                "api_calls": 7,
                "source": {
                    "kind": "provider_response",
                    "provider": "openai-codex",
                    "response_id": "resp_fixture_exact",
                    "usage_path": "response.usage",
                },
            },
            "cost": {
                "status": "available",
                "amount_micros": 18420,
                "currency": "USD",
                "source": {"kind": "provider_reported", "provider": "openai-codex"},
                "price_version": "provider-response-2026-07-27",
                "formula": "provider_reported_total",
            },
            "accepted_outcome": {
                "status": "accepted",
                "id": "client-signoff-20",
                "source": {"kind": "fixture", "reference": "acceptance-20"},
            },
        },
    }


def _unsupported_terminal():
    payload = _exact_terminal()
    payload.update({
        "dispatch_id": "webhook:cwc-issue-dispatch:telemetry-unsupported",
        "item_id": "coral-way-capital/demo#21",
        "issue_number": 21,
        "occurred_at": "2026-07-27T13:00:30+00:00",
        "pr_number": 221,
    })
    payload["telemetry"] = {
        "usage": {
            "status": "not_available",
            "source": {"kind": "provider_unsupported", "provider": "local-runner"},
        },
        "cost": {
            "status": "not_available",
            "source": {"kind": "usage_not_available", "provider": "local-runner"},
        },
        "accepted_outcome": {
            "status": "not_accepted",
            "source": {"kind": "fixture", "reference": "pending-review-21"},
        },
    }
    return payload


def test_exact_provider_fixture_and_duration_are_preserved():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "telemetry.db"
        dispatch_telemetry.record_dispatch_start(
            dispatch_id=DISPATCH_ID,
            item_id="coral-way-capital/demo#20",
            repo="coral-way-capital/demo",
            task_class="fix-bug",
            model_provider="openai-codex",
            model="gpt-5.5",
            started_at="2026-07-27T12:00:00+00:00",
            db_path=db_path,
        )
        normalized = worker_results.validate_worker_result(_exact_terminal())
        assert normalized["telemetry"]["usage"]["input_tokens"] == 1200
        dispatch_telemetry.record_terminal_result(normalized, db_path=db_path)

        row = dispatch_telemetry.get_dispatch(DISPATCH_ID, db_path=db_path)
        assert row["duration_ms"] == 123456
        assert row["duration_status"] == "available"
        assert row["input_tokens"] == 1200
        assert row["api_calls"] == 7
        assert row["cost_micros"] == 18420
        assert row["cost_price_version"] == "provider-response-2026-07-27"
        assert row["cost_formula"] == "provider_reported_total"
        assert row["usage_source"]["response_id"] == "resp_fixture_exact"


def test_unsupported_provider_is_not_available_and_never_estimated():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "telemetry.db"
        terminal = worker_results.validate_worker_result(_unsupported_terminal())
        dispatch_telemetry.record_dispatch_start(
            dispatch_id=terminal["dispatch_id"],
            item_id=terminal["item_id"],
            repo=terminal["repo"],
            task_class="review-harden",
            model_provider="local-runner",
            model="fixture",
            started_at="2026-07-27T13:00:00+00:00",
            db_path=db_path,
        )
        dispatch_telemetry.record_terminal_result(terminal, db_path=db_path)
        row = dispatch_telemetry.serialize_dispatch(
            dispatch_telemetry.get_dispatch(terminal["dispatch_id"], db_path=db_path)
        )
        assert row["duration_ms"] == 30000
        for field in (
            "input_tokens", "output_tokens", "cached_input_tokens",
            "reasoning_tokens", "total_tokens", "api_calls", "cost_micros",
        ):
            assert row[field] == "not_available"
        assert row["usage_source"]["kind"] == "provider_unsupported"


def test_heuristic_token_source_is_rejected():
    payload = _exact_terminal()
    payload["telemetry"]["usage"]["source"] = {
        "kind": "file_size_estimate",
        "content_bytes": 4800,
    }
    try:
        worker_results.validate_worker_result(payload)
    except worker_results.WorkerResultError as exc:
        assert "exact provider_response" in str(exc)
    else:
        raise AssertionError("heuristic token sources must never be accepted")


def test_structured_result_ingest_records_terminal_telemetry():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "queue-state.db"
        payload = _exact_terminal()
        queue = {
            "pending": [],
            "in_progress": [{
                "id": payload["item_id"],
                "repo": payload["repo"],
                "issue_number": payload["issue_number"],
                "dispatch_id": payload["dispatch_id"],
                "agent_started_at": "2026-07-27T12:00:00+00:00",
                "agent_prompt": "fix-bug",
                "model_provider": "openai-codex",
                "model": "gpt-5.5",
            }],
            "completed": [],
            "failed": [],
        }
        outcome = worker_results.ingest_worker_result(
            payload,
            source="fixture",
            db_path=db_path,
            telemetry_db_path=db_path,
            queue_loader=lambda: queue,
            queue_saver=lambda value: None,
            event_logger=lambda *args, **kwargs: None,
        )
        assert outcome["applied"]
        row = dispatch_telemetry.get_dispatch(DISPATCH_ID, db_path=db_path)
        assert row["duration_ms"] == 123456
        assert row["task_class"] == "fix-bug"
        assert row["model_provider"] == "openai-codex"


def test_aggregations_keep_pr_and_accepted_outcome_separate_and_auditable():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "telemetry.db"
        fixtures = (
            (
                _exact_terminal(),
                "fix-bug",
                "openai-codex",
                "gpt-5.5",
                "2026-07-27T12:00:00+00:00",
            ),
            (
                _unsupported_terminal(),
                "review-harden",
                "local-runner",
                "fixture",
                "2026-07-27T13:00:00+00:00",
            ),
        )
        for payload, task_class, provider, model, started_at in fixtures:
            result = worker_results.validate_worker_result(payload)
            dispatch_telemetry.record_dispatch_start(
                dispatch_id=result["dispatch_id"],
                item_id=result["item_id"],
                repo=result["repo"],
                task_class=task_class,
                model_provider=provider,
                model=model,
                started_at=started_at,
                db_path=db_path,
            )
            dispatch_telemetry.record_terminal_result(result, db_path=db_path)

        report = dispatch_telemetry.build_internal_report(db_path=db_path)
        assert len(report["by_dispatch"]) == 2
        assert set(report["by_repo"]) == {"coral-way-capital/demo"}
        assert set(report["by_task_class"]) == {"fix-bug", "review-harden"}
        assert set(report["by_model"]) == {"openai-codex/gpt-5.5", "local-runner/fixture"}
        assert set(report["by_pr"]) == {"coral-way-capital/demo#220", "coral-way-capital/demo#221"}
        assert set(report["by_accepted_outcome"]) == {"client-signoff-20"}
        assert report["summary"]["duration"]["status"] == "complete"
        assert report["summary"]["input_tokens"]["status"] == "partial"
        assert report["summary"]["cost"]["status"] == "partial"
        assert report["summary"]["cost"]["by_currency_micros"] == {"USD": 18420}

        public = dispatch_telemetry.build_public_completeness_report(db_path=db_path)
        assert public["dispatch_count"] == 2
        assert public["duration_complete_count"] == 2
        assert public["token_complete_count"] == 1
        assert public["cost_complete_count"] == 1
        assert "cost_micros" not in public
        assert "cost_basis" not in public


if __name__ == "__main__":
    test_exact_provider_fixture_and_duration_are_preserved()
    test_unsupported_provider_is_not_available_and_never_estimated()
    test_heuristic_token_source_is_rejected()
    test_structured_result_ingest_records_terminal_telemetry()
    test_aggregations_keep_pr_and_accepted_outcome_separate_and_auditable()
    print("ok")
