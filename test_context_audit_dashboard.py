#!/usr/bin/env python3
"""Dashboard contract tests for the repository-context audit surface."""

import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parent / "dashboard" / "index.html"


class ContextAuditDashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_context_audit_is_a_first_class_read_only_tab(self):
        self.assertIn("{ id: 'context-audit', label: 'Context audit'", self.html)
        self.assertIn("function ContextAuditView", self.html)
        self.assertIn("useFetch('/api/context-audit')", self.html)
        for forbidden in ("Run audit", "Scan repositories", "Rewrite context", "Remediate now"):
            self.assertNotIn(forbidden, self.html)

    def test_view_exposes_required_audit_hierarchy_and_filters(self):
        for phrase in (
            "Repository context coverage",
            "Threshold findings",
            "Score filter",
            "Drop filter",
            "Subscores",
            "Evidence",
            "Inventory revision",
            "Source observed",
        ):
            self.assertIn(phrase, self.html)

    def test_view_has_honest_unavailable_and_stale_states(self):
        self.assertIn("Audit reports unavailable", self.html)
        self.assertIn("REPORT STALE", self.html)
        self.assertIn("Read-only evidence", self.html)


if __name__ == "__main__":
    unittest.main()
