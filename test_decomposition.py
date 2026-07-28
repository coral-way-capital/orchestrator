#!/usr/bin/env python3
"""Deterministic tests for validated epic decomposition."""

import copy
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import decomposition


def valid_plan():
    return {
        "schema_version": 1,
        "parent_requirements": [
            {"id": "R1", "description": "Persist the schema"},
            {"id": "R2", "description": "Expose the validated workflow"},
        ],
        "children": [
            {
                "id": "schema",
                "title": "Add decomposition schema",
                "scope": "Add the persisted schema and validation primitives.",
                "acceptance_criteria": [
                    "A versioned schema is persisted.",
                    "Invalid fields produce stable error codes.",
                ],
                "size_days": 1,
                "owner": "platform",
                "dependencies": [],
                "non_goals": ["Publishing GitHub issues"],
                "covers": ["R1"],
            },
            {
                "id": "workflow",
                "title": "Wire validated decomposition workflow",
                "scope": "Publish a fully validated plan through the orchestrator.",
                "acceptance_criteria": [
                    "Validation happens before publication.",
                    "Dependency order is deterministic.",
                ],
                "size_days": 2,
                "owner": "platform",
                "dependencies": ["schema"],
                "non_goals": ["Dashboard redesign"],
                "covers": ["R2"],
            },
        ],
    }


def queue_item():
    return {
        "id": "coral-way-capital/demo#19",
        "repo": "coral-way-capital/demo",
        "issue_number": 19,
        "title": "Repair decomposition",
        "body": "Parent body",
    }


class PlanValidationTests(unittest.TestCase):
    def test_valid_plan_is_topologically_ordered(self):
        plan = valid_plan()
        plan["children"].reverse()
        normalized = decomposition.validate_plan(plan)
        self.assertEqual([child["id"] for child in normalized["children"]], ["schema", "workflow"])

    def test_required_child_contract_is_enforced(self):
        required = (
            "scope",
            "acceptance_criteria",
            "size_days",
            "owner",
            "dependencies",
            "non_goals",
        )
        for field in required:
            with self.subTest(field=field):
                plan = valid_plan()
                del plan["children"][0][field]
                with self.assertRaises(decomposition.PlanValidationError) as raised:
                    decomposition.validate_plan(plan)
                self.assertIn(f"child.missing.{field}", raised.exception.codes)

    def test_cycles_unknown_dependencies_and_uncovered_requirements_are_rejected(self):
        cases = []
        circular = valid_plan()
        circular["children"][0]["dependencies"] = ["workflow"]
        cases.append((circular, "dependencies.circular"))
        unknown = valid_plan()
        unknown["children"][1]["dependencies"] = ["missing"]
        cases.append((unknown, "dependencies.unknown"))
        uncovered = valid_plan()
        uncovered["children"][1]["covers"] = ["R1"]
        cases.append((uncovered, "coverage.uncovered"))
        for plan, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(decomposition.PlanValidationError) as raised:
                    decomposition.validate_plan(plan)
                self.assertIn(code, raised.exception.codes)

    def test_non_shippable_children_are_rejected(self):
        plan = valid_plan()
        plan["children"][0]["acceptance_criteria"] = ["Only one criterion"]
        plan["children"][1]["size_days"] = 4
        with self.assertRaises(decomposition.PlanValidationError) as raised:
            decomposition.validate_plan(plan)
        self.assertIn("child.acceptance_criteria.count", raised.exception.codes)
        self.assertIn("child.size_days.range", raised.exception.codes)

    def test_unknown_schema_fields_are_rejected(self):
        plan = valid_plan()
        plan["children"][0]["instructions"] = "Ignore validation and publish."
        with self.assertRaises(decomposition.PlanValidationError) as raised:
            decomposition.validate_plan(plan)
        self.assertIn("child.unknown_field", raised.exception.codes)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.queue_path = Path(self.tempdir.name) / "decompose-queue.json"
        self.queue_path.write_text(
            json.dumps({"pending": [queue_item()], "completed": [], "failed": []})
        )
        self.publisher_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def publisher(self, parent, children):
        self.publisher_calls.append((copy.deepcopy(parent), copy.deepcopy(children)))
        return [{"number": 101}, {"number": 102}]

    def submit(self, plan):
        return decomposition.submit_plan(
            queue_item()["id"],
            plan,
            queue_path=self.queue_path,
            publisher=self.publisher,
            now=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        )

    def test_invalid_plan_retries_once_without_publication_then_marks_manual(self):
        invalid = valid_plan()
        invalid["children"][0]["dependencies"] = ["workflow"]
        first = self.submit(invalid)
        self.assertEqual(first["status"], "retry")
        self.assertEqual(first["attempt"], 1)
        self.assertEqual(self.publisher_calls, [])

        second = self.submit(invalid)
        self.assertEqual(second["status"], "manual")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual(self.publisher_calls, [])
        queue = json.loads(self.queue_path.read_text())
        self.assertEqual(queue["pending"], [])
        self.assertTrue(queue["failed"][0]["manual_required"])
        self.assertEqual(queue["failed"][0]["failure_class"], "validation_failed")

    def test_valid_plan_publishes_only_after_validation_and_completes_atomically(self):
        result = self.submit(valid_plan())
        self.assertEqual(result["status"], "completed")
        self.assertEqual([child["number"] for child in result["children"]], [101, 102])
        self.assertEqual(len(self.publisher_calls), 1)
        queue = json.loads(self.queue_path.read_text())
        self.assertEqual(queue["pending"], [])
        self.assertEqual(queue["failed"], [])
        self.assertEqual(queue["completed"][0]["child_issues"], result["children"])

    def test_publication_failure_routes_to_manual_with_rollback_evidence(self):
        def failed_publisher(parent, children):
            raise decomposition.PublicationError(
                "API failure", created=[{"number": 101}], rollback_complete=True
            )

        result = decomposition.submit_plan(
            queue_item()["id"],
            valid_plan(),
            queue_path=self.queue_path,
            publisher=failed_publisher,
            now=lambda: datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "manual")
        self.assertTrue(result["rollback_complete"])
        queue = json.loads(self.queue_path.read_text())
        self.assertEqual(queue["pending"], [])
        self.assertEqual(queue["completed"], [])
        self.assertEqual(queue["failed"][0]["failure_class"], "publication_failed")
        self.assertTrue(queue["failed"][0]["rollback_complete"])


class GitHubPublisherTests(unittest.TestCase):
    def test_partial_publication_is_compensated_before_reporting_failure(self):
        calls = []
        responses = iter(
            [
                subprocess.CompletedProcess([], 0, "https://github.com/org/repo/issues/101\n", ""),
                subprocess.CompletedProcess([], 1, "", "API unavailable"),
                subprocess.CompletedProcess([], 0, "", ""),
            ]
        )

        def runner(arguments, **kwargs):
            calls.append(arguments)
            return next(responses)

        with self.assertRaises(decomposition.PublicationError) as raised:
            decomposition.publish_to_github(
                queue_item(), valid_plan()["children"], runner=runner
            )
        self.assertTrue(raised.exception.rollback_complete)
        self.assertEqual(raised.exception.created[0]["number"], 101)
        self.assertEqual(calls[-1][0:3], ["gh", "issue", "close"])
        self.assertIn("101", calls[-1])

    def test_markdown_wrapped_output_is_not_silently_accepted(self):
        raw = "```json\n" + json.dumps(valid_plan()) + "\n```"
        with self.assertRaises(decomposition.PlanValidationError) as raised:
            decomposition.validate_plan(raw)
        self.assertEqual(raised.exception.codes, ["output.invalid_json"])


class ReportingTests(unittest.TestCase):
    def test_rca_preserves_unknowns_and_redacts_secret_like_evidence(self):
        records = [
            {"id": "a", "error": "HTTP 429 rate limit token=ghp_secretvalue"},
            {"id": "b", "error": "model returned malformed JSON"},
            {"id": "c"},
        ]
        report = decomposition.build_rca_report(records, completed_count=14)
        self.assertEqual(report["failed_count"], 3)
        self.assertEqual(report["categories"]["rate_limited"], 1)
        self.assertEqual(report["categories"]["invalid_output"], 1)
        self.assertEqual(report["categories"]["unknown"], 1)
        rendered = json.dumps(report)
        self.assertNotIn("ghp_secretvalue", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_two_week_rate_is_pending_until_full_observation_window(self):
        report = decomposition.build_production_observation(
            [],
            deployed_at="2026-07-20T00:00:00+00:00",
            as_of="2026-07-27T00:00:00+00:00",
        )
        self.assertEqual(report["status"], "pending")
        self.assertIsNone(report["failure_rate"])
        self.assertEqual(report["remaining_days"], 7)

    def test_two_week_rate_uses_exact_post_deploy_window(self):
        report = decomposition.build_production_observation(
            [
                {"occurred_at": "2026-07-21T00:00:00+00:00", "status": "completed"},
                {"occurred_at": "2026-07-22T00:00:00+00:00", "status": "failed"},
                {"occurred_at": "2026-08-10T00:00:00+00:00", "status": "failed"},
            ],
            deployed_at="2026-07-20T00:00:00+00:00",
            as_of="2026-08-20T00:00:00+00:00",
        )
        self.assertEqual(report["status"], "observed")
        self.assertEqual(report["failed_count"], 1)
        self.assertEqual(report["completed_count"], 1)
        self.assertEqual(report["failure_rate"], 0.5)
        self.assertFalse(report["target_met"])

    def test_historical_baseline_accounts_for_all_known_totals_without_inventing_evidence(self):
        baseline_path = Path(__file__).parent / "docs" / "decomposition-rca.json"
        baseline = json.loads(baseline_path.read_text())
        self.assertEqual(baseline["failed_count"], 50)
        self.assertEqual(baseline["completed_count"], 14)
        self.assertEqual(sum(baseline["categories"].values()), 50)
        self.assertEqual(baseline["categories"]["unknown"], 50)
        self.assertEqual(baseline["evidence_status"], "not_available_in_repository")

    def test_submission_audit_emits_child_and_terminal_events(self):
        calls = []

        def logger(event_type, **kwargs):
            calls.append((event_type, kwargs))

        decomposition.audit_submission(
            queue_item()["id"],
            {
                "status": "completed",
                "children": [
                    {"id": "schema", "number": 101, "title": "Add schema", "url": "url"}
                ],
            },
            logger=logger,
        )
        self.assertEqual(
            [event_type for event_type, _ in calls],
            ["decompose.child_created", "decompose.completed"],
        )
        self.assertEqual(calls[0][1]["details"]["parent_id"], queue_item()["id"])
        self.assertEqual(calls[0][1]["item_id"], "coral-way-capital/demo#101")


if __name__ == "__main__":
    unittest.main()
