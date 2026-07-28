#!/usr/bin/env python3
"""Outcome trace and funnel tests for issue #23."""

import json
import unittest
from pathlib import Path

import outcome_traces


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "outcome_traces.json"


class OutcomeTraceTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

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
                        for stage in outcome_traces.STAGES
                    )
                )
                self.assertEqual(
                    set(trace["cycle_times_seconds"]),
                    set(outcome_traces.STAGE_TRANSITIONS),
                )
                self.assertEqual(trace["outcome_contract"]["contract_version"], 1)
                self.assertTrue(trace["outcome_contract"]["project_id"])
                self.assertTrue(trace["outcome_contract"]["evidence_requirement"])
                self.assertTrue(trace["outcome_contract"]["approval_boundary"])

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
        self.assertEqual(report["funnel"]["accepted"]["count"], 1)

    def test_accepted_requires_separate_reviewer_and_evidence_provenance(self):
        invalid = json.loads(json.dumps(self.payload))
        invalid["traces"][0]["acceptance"]["reviewer"] = None
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "reviewer"):
            outcome_traces.build_funnel(invalid)

    def test_sensitive_evidence_content_is_rejected(self):
        invalid = json.loads(json.dumps(self.payload))
        invalid["traces"][0]["acceptance"]["evidence_source"]["raw_content"] = (
            "confidential"
        )
        with self.assertRaisesRegex(outcome_traces.OutcomeTraceError, "references only"):
            outcome_traces.build_funnel(invalid)


if __name__ == "__main__":
    unittest.main()
