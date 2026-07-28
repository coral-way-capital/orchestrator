#!/usr/bin/env python3
"""Outcome trace and funnel tests for issue #23."""

import json
import tempfile
import unittest
from pathlib import Path

import outcome_traces


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "outcome_traces.json"


class OutcomeTraceTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def accepted_payload(self):
        payload = json.loads(json.dumps(self.payload))
        trace = payload["traces"][0]
        trace["stages"]["accepted"] = {
            "reference": "synthetic://evidence/rsm-acceptance-001",
            "occurred_at": "2026-07-03T15:00:00Z",
        }
        trace["acceptance"] = {
            "reviewer": {
                "reference": "synthetic://reviewers/rsm-primary",
                "role": "Named RSM reviewer",
            },
            "status": "accepted",
            "evidence_source": {
                "source_type": "client_confirmation",
                "reference": "synthetic://evidence/rsm-acceptance-001",
            },
            "observed_at": "2026-07-03T14:55:00Z",
            "accepted_at": "2026-07-03T15:00:00Z",
            "provenance": {
                "system": "synthetic_fixture",
                "recorded_at": "2026-07-03T15:05:00Z",
                "record_reference": "synthetic://records/rsm-acceptance-001",
            },
        }
        return payload

    def test_rsm_and_zenna_cover_the_full_trace(self):
        report = outcome_traces.build_funnel(self.payload)
        self.assertEqual(
            {trace["client_slug"] for trace in report["traces"]}, {"rsm", "zenna"}
        )
        for trace in report["traces"]:
            with self.subTest(trace=trace["trace_id"]):
                self.assertTrue(
                    all(
                        trace["stages"][stage]["reference"]
                        for stage in outcome_traces.STAGES[:-1]
                    )
                )
                self.assertIsNone(trace["stages"]["accepted"]["reference"])
                self.assertEqual(trace["acceptance"]["status"], "unknown")
                self.assertEqual(
                    set(trace["cycle_times_seconds"]),
                    set(outcome_traces.STAGE_TRANSITIONS),
                )
                self.assertEqual(
                    trace["project_contract"]["version"], 2
                )
                self.assertTrue(trace["project_contract"]["project_id"])
                self.assertEqual(
                    set(trace["project_contract"]), {"version", "project_id"}
                )
                self.assertNotIn("outcome_contract", trace)

    def test_merge_does_not_create_acceptance(self):
        zenna = next(
            trace for trace in self.payload["traces"] if trace["client_slug"] == "zenna"
        )
        self.assertIsNotNone(zenna["stages"]["merged"]["occurred_at"])
        self.assertEqual(zenna["acceptance"]["status"], "unknown")
        report = outcome_traces.build_funnel(self.payload)
        zenna_report = next(
            trace for trace in report["traces"] if trace["client_slug"] == "zenna"
        )
        self.assertIsNone(zenna_report["stages"]["accepted"]["occurred_at"])
        self.assertEqual(report["funnel"]["accepted"]["count"], 0)

    def test_accepted_requires_separate_reviewer_and_evidence_provenance(self):
        invalid = self.accepted_payload()
        invalid["traces"][0]["acceptance"]["reviewer"] = None
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "reviewer"):
            outcome_traces.build_funnel(invalid)

    def test_sensitive_evidence_content_is_rejected(self):
        invalid = self.accepted_payload()
        invalid["traces"][0]["acceptance"]["evidence_source"]["raw_content"] = (
            "confidential"
        )
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "references only"):
            outcome_traces.build_funnel(invalid)

    def test_unknown_acceptance_allows_canonical_null_provenance(self):
        canonical = json.loads(json.dumps(self.payload))
        zenna = next(
            trace for trace in canonical["traces"] if trace["client_slug"] == "zenna"
        )
        zenna["acceptance"]["provenance"] = None
        report = outcome_traces.build_funnel(canonical)
        zenna_report = next(
            trace for trace in report["traces"] if trace["client_slug"] == "zenna"
        )
        self.assertIsNone(zenna_report["acceptance"]["provenance"])

    def test_references_are_bounded_and_cannot_expose_local_paths(self):
        for reference in (
            "/home/deploy/.hermes/issue-queue/evidence.json",
            "x" * (outcome_traces.MAX_REFERENCE_LENGTH + 1),
        ):
            invalid = json.loads(json.dumps(self.payload))
            invalid["traces"][0]["stages"]["issue"]["reference"] = reference
            with self.subTest(reference=reference[:30]), self.assertRaisesRegex(
                outcome_traces.OutcomeTraceError, "reference"
            ):
                outcome_traces.build_funnel(invalid)

    def test_evidence_source_type_is_canonical_allowlist(self):
        invalid = self.accepted_payload()
        invalid["traces"][0]["acceptance"]["evidence_source"]["source_type"] = (
            "raw_email"
        )
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "source_type"):
            outcome_traces.build_funnel(invalid)

    def test_acceptance_reviewer_must_match_canonical_project_authority(self):
        invalid = self.accepted_payload()
        projects = {
            "rsm-eckhart": {
                "approval_boundary": {
                    "acceptance_authority": "Different authority"
                }
            },
            "zenna-crm": {
                "approval_boundary": {
                    "acceptance_authority": "Named Zenna reviewer"
                }
            },
        }
        with self.assertRaisesRegex(
            outcome_traces.OutcomeTraceError, "acceptance authority"
        ):
            outcome_traces.build_funnel(invalid, projects_by_id=projects)

    def test_filter_order_is_deterministic_and_trace_ids_are_unique(self):
        reversed_payload = json.loads(json.dumps(self.payload))
        reversed_payload["traces"].reverse()
        report = outcome_traces.build_funnel(reversed_payload)
        self.assertEqual(
            [trace["trace_id"] for trace in report["traces"]],
            sorted(trace["trace_id"] for trace in report["traces"]),
        )

        duplicate = json.loads(json.dumps(self.payload))
        duplicate["traces"][1]["trace_id"] = duplicate["traces"][0]["trace_id"]
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "duplicate"):
            outcome_traces.build_funnel(duplicate)

    def test_load_funnel_does_not_expose_its_local_source_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "outcome-traces.json"
            path.write_text(json.dumps(self.payload), encoding="utf-8")
            report = outcome_traces.load_funnel(path)
        self.assertNotIn("source", report)


if __name__ == "__main__":
    unittest.main()
