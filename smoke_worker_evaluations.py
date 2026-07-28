#!/usr/bin/env python3
"""Deterministic evaluation registry and feedback guard checks (issue #18)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import dispatch_telemetry
import worker_evaluations
import pr_outcomes


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "worker_evaluations.json").read_text(
        encoding="utf-8"
    )
)


def test_terminal_fixture_coverage_and_evidence_provenance():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "evaluations.db"
        report = worker_evaluations.refresh_registry(
            FIXTURE["pull_requests"],
            db_path=db,
            evaluated_at=FIXTURE["evaluated_at"],
        )

        assert report["coverage"] == {
            "terminal_tracked": 3,
            "evaluated": 3,
            "percent": 100.0,
        }
        assert [row["pr_number"] for row in report["evaluations"]] == [17, 18, 19]

        merged = report["evaluations"][0]
        assert merged["merge_state"]["value"] == "merged"
        assert merged["time_to_merge"]["seconds"] == 192600
        assert merged["review_severity"]["value"] == "high"
        assert merged["review_severity"]["evidence"][0]["url"].endswith(
            "#pullrequestreview-171"
        )
        assert merged["fix_up_ratio"]["value"] == 0.5
        assert merged["fix_up_ratio"]["numerator"] == 1
        assert merged["fix_up_ratio"]["denominator"] == 2
        assert merged["fix_up_ratio"]["evidence"][0]["url"].endswith(
            "2222222222222222222222222222222222222222"
        )
        assert merged["reopen_or_follow_up"]["value"] == "none_observed"
        assert merged["human_override"]["value"] == "none_observed"

        closed = report["evaluations"][1]
        assert closed["review_severity"]["value"] == "not_available"
        assert closed["fix_up_ratio"]["value"] == "not_available"
        assert closed["time_to_merge"]["seconds"] is None
        assert closed["time_to_merge"]["availability"] == "not_available"
        assert closed["reopen_or_follow_up"]["value"] == "reopened_and_follow_up"
        assert closed["human_override"]["value"] == "wrong_direction"
        evidence_urls = {
            finding["evidence"][0]["url"]
            for finding in (
                closed["reopen_or_follow_up"],
                closed["human_override"],
            )
        }
        assert evidence_urls == {
            "https://github.com/coral-way-capital/cwc-control-plane/issues/18#event-1801",
            "https://github.com/coral-way-capital/orchestrator/pull/18#issuecomment-1802",
        }


def test_refresh_is_idempotent_and_order_is_stable():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "evaluations.db"
        reversed_pulls = list(reversed(FIXTURE["pull_requests"]))
        first = worker_evaluations.refresh_registry(
            reversed_pulls, db_path=db, evaluated_at=FIXTURE["evaluated_at"]
        )
        second = worker_evaluations.refresh_registry(
            FIXTURE["pull_requests"],
            db_path=db,
            evaluated_at="2026-07-28T12:00:00Z",
        )
        shuffled_evidence = json.loads(json.dumps(FIXTURE["pull_requests"]))
        shuffled_evidence[0]["reviews"].reverse()
        shuffled_evidence[0]["commits"].reverse()
        worker_evaluations.refresh_registry(
            shuffled_evidence,
            db_path=db,
            evaluated_at="2026-07-29T12:00:00Z",
        )
        assert first["evaluations"][0]["evaluated_at"] != second["evaluations"][0]["evaluated_at"]
        assert len(worker_evaluations.list_evaluations(db_path=db)) == 3
        assert worker_evaluations.list_evaluations(db_path=db) == first["evaluations"]
        with worker_evaluations.get_db(db) as connection:
            history_count = connection.execute(
                "SELECT COUNT(*) FROM worker_evaluation_history"
            ).fetchone()[0]
        assert history_count == 3


def test_pr_outcome_adapter_evaluates_every_terminal_row_without_inference():
    with tempfile.TemporaryDirectory() as td:
        pr_db = Path(td) / "pr-outcomes.db"
        telemetry_db = Path(td) / "telemetry.db"
        evaluation_db = Path(td) / "evaluations.db"
        queue_file = Path(td) / "queue.json"
        queue_file.write_text(
            json.dumps(
                {
                    "pending": [],
                    "in_progress": [],
                    "failed": [],
                    "completed": [
                        {
                            "id": "coral-way-capital/cwc-control-plane#17",
                            "dispatch_id": "webhook:cwc-issue-dispatch:evaluation-17",
                            "agent_prompt": "fix-bug",
                            "model_provider": "requested-provider",
                            "model": "requested-model",
                            "task_class": "bug",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        for pull in FIXTURE["pull_requests"][:2]:
            reviews = [dict(review) for review in pull["reviews"]]
            if reviews:
                reviews[0]["body"] += " token=must-not-be-persisted"
            pr_outcomes.ingest_pull_snapshot(
                pull["repo"],
                {
                    "id": 17000 + pull["pr_number"],
                    "number": pull["pr_number"],
                    "html_url": (
                        f"https://github.com/{pull['repo']}/pull/{pull['pr_number']}"
                    ),
                    "state": "closed",
                    "created_at": pull["opened_at"],
                    "updated_at": pull["closed_at"],
                    "closed_at": pull["closed_at"],
                    "merged_at": pull["merged_at"],
                },
                reviews,
                db_path=pr_db,
                context_resolver=lambda repo, number: {
                    "item_id": (
                        "coral-way-capital/cwc-control-plane#17"
                        if number == 17
                        else None
                    ),
                    "dispatch_id": (
                        "webhook:cwc-issue-dispatch:evaluation-17"
                        if number == 17
                        else None
                    ),
                    "project_id": None,
                    "worker_result_id": 7001 if number == 17 else None,
                    "linkage_state": "linked" if number == 17 else "not_available",
                },
            )
        dispatch_telemetry.record_dispatch_start(
            dispatch_id="webhook:cwc-issue-dispatch:evaluation-17",
            item_id="coral-way-capital/cwc-control-plane#17",
            repo="coral-way-capital/orchestrator",
            task_class="bug",
            model_provider="requested-provider",
            model="requested-model",
            started_at="2026-07-01T10:00:00Z",
            db_path=telemetry_db,
        )
        dispatch_telemetry.record_terminal_result(
            {
                "dispatch_id": "webhook:cwc-issue-dispatch:evaluation-17",
                "item_id": "coral-way-capital/cwc-control-plane#17",
                "repo": "coral-way-capital/orchestrator",
                "status": "completed",
                "occurred_at": "2026-07-03T15:30:00Z",
                "pr_number": 17,
                "telemetry": {
                    "usage": {
                        "status": "available",
                        "input_tokens": 1200,
                        "output_tokens": 345,
                        "source": {
                            "kind": "provider_response",
                            "provider": "openai-codex",
                            "model": "gpt-5.5",
                            "response_id": "resp-evaluation-17",
                            "usage_path": "response.usage",
                        },
                    },
                    "cost": {
                        "status": "not_available",
                        "source": {
                            "kind": "provider_unsupported",
                            "provider": "openai-codex",
                        },
                    },
                    "accepted_outcome": {
                        "status": "not_available",
                        "source": {"kind": "not_reported"},
                    },
                },
            },
            db_path=telemetry_db,
        )
        review_events = pr_outcomes.list_pull_events(
            FIXTURE["pull_requests"][0]["repo"], 17, db_path=pr_db
        )
        change_payload = next(
            row["payload"]
            for row in review_events
            if row["event_type"] == "changes_requested"
        )
        assert change_payload["severity_tag"] == "high"
        assert "body" not in change_payload
        assert "must-not-be-persisted" not in json.dumps(change_payload)
        dismissed_pull = {
            "id": 17020,
            "number": 20,
            "html_url": "https://github.com/coral-way-capital/orchestrator/pull/20",
            "state": "closed",
            "created_at": "2026-07-02T00:00:00Z",
            "updated_at": "2026-07-04T00:00:00Z",
            "closed_at": "2026-07-04T00:00:00Z",
            "merged_at": "2026-07-04T00:00:00Z",
        }
        pr_outcomes.ingest_pull_snapshot(
            "coral-way-capital/orchestrator",
            dismissed_pull,
            [
                {
                    "id": 201,
                    "state": "CHANGES_REQUESTED",
                    "submitted_at": "2026-07-02T01:00:00Z",
                    "body": "[severity:critical] Superseded finding.",
                    "html_url": "https://github.com/coral-way-capital/orchestrator/pull/20#pullrequestreview-201",
                    "commit_id": "3333333333333333333333333333333333333333",
                }
            ],
            db_path=pr_db,
            context_resolver=lambda repo, number: {
                "item_id": None,
                "dispatch_id": None,
                "project_id": None,
                "worker_result_id": None,
                "linkage_state": "not_available",
            },
        )
        pr_outcomes.ingest_pull_snapshot(
            "coral-way-capital/orchestrator",
            dismissed_pull,
            [
                {
                    "id": 201,
                    "state": "DISMISSED",
                    "submitted_at": "2026-07-03T01:00:00Z",
                    "body": None,
                    "html_url": "https://github.com/coral-way-capital/orchestrator/pull/20#pullrequestreview-201",
                    "commit_id": "3333333333333333333333333333333333333333",
                }
            ],
            db_path=pr_db,
            context_resolver=lambda repo, number: {
                "item_id": None,
                "dispatch_id": None,
                "project_id": None,
                "worker_result_id": None,
                "linkage_state": "not_available",
            },
        )
        report = worker_evaluations.refresh_from_pr_outcomes(
            pr_db_path=pr_db,
            db_path=evaluation_db,
            queue_file=queue_file,
            telemetry_db_path=telemetry_db,
            evaluated_at=FIXTURE["evaluated_at"],
        )
        assert report["coverage"]["percent"] == 100.0
        assert report["coverage"]["terminal_tracked"] == 3
        assert report["evaluations"][0]["prompt_id"] == "fix-bug"
        assert report["evaluations"][0]["model_provider"] == "openai-codex"
        assert report["evaluations"][0]["model"] == "gpt-5.5"
        assert report["evaluations"][0]["task_class"] == "bug"
        provenance = report["evaluations"][0]["provenance"]
        assert provenance["queue_item"]["value"] == (
            "coral-way-capital/cwc-control-plane#17"
        )
        assert provenance["queue_item"]["availability"] == "available"
        assert provenance["dispatch"]["value"] == (
            "webhook:cwc-issue-dispatch:evaluation-17"
        )
        assert provenance["structured_result"]["value"] == 7001
        assert provenance["pull_request"]["value"].endswith("#17")
        assert provenance["linkage_state"] == "linked"
        assert report["evaluations"][0]["review_severity"]["evidence"][0]["url"].endswith(
            "#pullrequestreview-171"
        )
        assert report["evaluations"][0]["fix_up_ratio"]["value"] == "not_available"
        assert report["evaluations"][0]["human_override"]["value"] == "not_available"
        metrics = report["evaluations"][0]["metrics"]
        assert metrics["review_cycles"] == {"value": 1, "availability": "available"}
        assert metrics["review_delay_seconds"] == {
            "value": 7200,
            "availability": "available",
        }
        assert metrics["build"]["availability"] == "not_available"
        assert metrics["build"]["reason"] == (
            "build metrics are absent from the PR outcome ledger"
        )
        assert metrics["size"]["availability"] == "not_available"
        assert metrics["size"]["reason"] == (
            "commit and diff size metrics are absent from the PR outcome ledger"
        )
        assert report["evaluations"][0]["accepted_outcome"] == {
            "value": "not_available",
            "availability": "not_available",
            "reason": "PR engineering state is not accepted business outcome evidence",
        }
        telemetry_unavailable = worker_evaluations.refresh_from_pr_outcomes(
            pr_db_path=pr_db,
            db_path=Path(td) / "evaluations-without-telemetry.db",
            queue_file=queue_file,
            telemetry_db_path=Path(td) / "missing" / "telemetry.db",
            evaluated_at=FIXTURE["evaluated_at"],
        )
        assert telemetry_unavailable["coverage"]["percent"] == 100.0
        assert telemetry_unavailable["evaluations"][0]["model_provider"] == (
            "requested-provider"
        )
        assert telemetry_unavailable["evaluations"][0]["model"] == "requested-model"
        dismissed = report["evaluations"][2]
        assert dismissed["pr_number"] == 20
        assert dismissed["review_severity"]["availability"] == "not_available"
        assert dismissed["metrics"]["review_cycles"] == {
            "value": 0,
            "availability": "available",
        }


def test_weekly_digest_is_stable_and_recommends_without_routing():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "evaluations.db"
        worker_evaluations.refresh_registry(
            FIXTURE["pull_requests"],
            db_path=db,
            evaluated_at=FIXTURE["evaluated_at"],
        )
        digest = worker_evaluations.build_weekly_digest(
            week_start=FIXTURE["week_start"], db_path=db
        )

        assert digest["top_repeated_failure"] == {
            "failure": "wrong_direction",
            "count": 2,
            "prompt_id": "fix-bug",
            "model_provider": "openai-codex",
            "model": "gpt-5.5",
            "task_class": "bug",
        }
        assert digest["proposed_system_change"]["type"] == "prompt_review"
        assert "wrong-direction" in digest["proposed_system_change"]["description"]
        assert digest["routing"]["mode"] == "recommendations_only"
        assert digest["routing"]["automatic_mutation"] is False
        assert digest["sample"]["eligible_failures"] == 3
        assert digest["sample"]["excluded_unavailable_context"] == 0


def test_unavailable_context_cannot_rank_or_recommend_and_offsets_use_exact_instants():
    pulls = [dict(row) for row in FIXTURE["pull_requests"][1:]]
    for index, pull in enumerate(pulls, start=1):
        pull = dict(pull)
        pull["pr_number"] = 100 + index
        pull["prompt_id"] = None
        pull["terminal_at"] = None
        pull["closed_at"] = f"2026-07-09T0{index}:00:00+02:00"
        pulls[index - 1] = pull
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "evaluations.db"
        worker_evaluations.refresh_registry(
            pulls, db_path=db, evaluated_at=FIXTURE["evaluated_at"]
        )
        digest = worker_evaluations.build_weekly_digest(
            week_start="2026-07-02T00:00:00Z", db_path=db
        )
        assert digest["top_repeated_failure"] is None
        assert digest["proposed_system_change"]["status"] == "no_change_proposed"
        assert digest["sample"]["excluded_unavailable_context"] == 2


def test_automatic_routing_requires_elapsed_gate_and_ivan_approval():
    observed_at = "2026-07-01T00:00:00Z"
    before_30_days = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-07-30T23:59:59Z",
        approval={
            "actor": "ivanacostarubio",
            "approved_at": "2026-07-02T00:00:00Z",
            "evidence_url": "https://github.com/coral-way-capital/cwc-control-plane/issues/18#issuecomment-1",
        },
    )
    no_approval = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
    )
    wrong_approver = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={
            "actor": "someone-else",
            "approved_at": "2026-08-01T00:00:00Z",
            "evidence_url": "https://github.com/coral-way-capital/cwc-control-plane/issues/18#issuecomment-2",
        },
    )
    approved = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={
            "actor": "ivanacostarubio",
            "approved_at": "2026-08-01T00:00:00Z",
            "evidence_url": "https://github.com/coral-way-capital/cwc-control-plane/issues/18#issuecomment-3",
        },
    )
    future_approval = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={
            "actor": "ivanacostarubio",
            "approved_at": "2026-09-01T00:00:00Z",
            "evidence_url": "https://github.com/coral-way-capital/cwc-control-plane/issues/18#issuecomment-4",
        },
    )
    premature_approval = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={
            "actor": "ivanacostarubio",
            "approved_at": "2026-07-15T00:00:00Z",
            "evidence_url": "https://github.com/coral-way-capital/cwc-control-plane/issues/18#issuecomment-5",
        },
    )
    unproven_approval = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={
            "actor": "ivanacostarubio",
            "approved_at": "2026-08-01T00:00:00Z",
        },
    )

    assert before_30_days["automatic_routing_enabled"] is False
    assert before_30_days["observation_period_complete"] is False
    assert no_approval["automatic_routing_enabled"] is False
    assert no_approval["approval_state"] == "not_available"
    assert wrong_approver["automatic_routing_enabled"] is False
    assert wrong_approver["approval_state"] == "invalid_approver"
    assert approved["automatic_routing_enabled"] is False
    assert approved["approval_state"] == "approved_boundary_only"
    assert approved["reason"] == "automatic routing remains disabled in code"
    assert future_approval["approval_state"] == "invalid_future_approval"
    assert premature_approval["approval_state"] == "invalid_premature_approval"
    assert unproven_approval["approval_state"] == "invalid_missing_evidence"


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"PASS smoke_worker_evaluations ({len(tests)} tests)")
