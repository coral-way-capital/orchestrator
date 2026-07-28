#!/usr/bin/env python3
"""Dashboard contract for the read-only accepted-outcome funnel."""

import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parent / "dashboard" / "index.html"


class OutcomeDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_outcomes_are_a_read_only_first_class_view(self):
        self.assertIn("{ id: 'outcomes', label: 'Outcomes'", self.html)
        self.assertIn("function OutcomeFunnelView", self.html)
        self.assertIn("useFetch('/api/outcome-funnel')", self.html)
        self.assertIn("Read-only acceptance evidence", self.html)

    def test_view_exposes_full_funnel_and_provenance(self):
        for phrase in (
            "Project → issue → run → PR → reviewed → merged → accepted",
            "Evidence reference",
            "Provenance",
            "Unknown remains unknown",
        ):
            self.assertIn(phrase, self.html)


if __name__ == "__main__":
    unittest.main()
