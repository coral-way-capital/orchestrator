#!/usr/bin/env python3
"""Deterministic evaluation registry and feedback guard checks (issue #18)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

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
            FIXTURE["pull_requests"], db_path=db, evaluated_at=FIXTURE["evaluated_at"]
        )
        assert first == second
        assert len(worker_evaluations.list_evaluations(db_path=db)) == 3


def test_pr_outcome_adapter_evaluates_every_terminal_row_without_inference():
    with tempfile.TemporaryDirectory() as td:
        pr_db = Path(td) / "pr-outcomes.db"
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
                            "agent_prompt": "fix-bug",
                            "model_provider": "openai-codex",
                            "model": "gpt-5.5",
                            "task_class": "bug",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        for pull in FIXTURE["pull_requests"][:2]:
            reviews = pull["reviews"]
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
                    "dispatch_id": None,
                    "project_id": None,
                    "worker_result_id": None,
                    "linkage_state": "linked" if number == 17 else "not_available",
                },
            )
        report = worker_evaluations.refresh_from_pr_outcomes(
            pr_db_path=pr_db,
            db_path=evaluation_db,
            queue_file=queue_file,
            evaluated_at=FIXTURE["evaluated_at"],
        )
        assert report["coverage"]["percent"] == 100.0
        assert report["coverage"]["terminal_tracked"] == 2
        assert report["evaluations"][0]["prompt_id"] == "fix-bug"
        assert report["evaluations"][0]["review_severity"]["evidence"][0]["url"].endswith(
            "#pullrequestreview-171"
        )
        assert report["evaluations"][0]["fix_up_ratio"]["value"] == "not_available"
        assert report["evaluations"][0]["human_override"]["value"] == "not_available"


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


def test_automatic_routing_requires_elapsed_gate_and_ivan_approval():
    observed_at = "2026-07-01T00:00:00Z"
    before_30_days = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-07-30T23:59:59Z",
        approval={"actor": "ivan", "approved_at": "2026-07-02T00:00:00Z"},
    )
    no_approval = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
    )
    wrong_approver = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={"actor": "someone-else", "approved_at": "2026-08-01T00:00:00Z"},
    )
    approved = worker_evaluations.routing_gate(
        observed_at=observed_at,
        now="2026-08-01T00:00:00Z",
        approval={"actor": "ivan", "approved_at": "2026-08-01T00:00:00Z"},
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
