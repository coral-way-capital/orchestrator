#!/usr/bin/env python3
"""Dashboard contract tests for the Portfolio decision surface."""

import unittest
from pathlib import Path


DASHBOARD = Path(__file__).resolve().parent / "dashboard" / "index.html"


class PortfolioDashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DASHBOARD.read_text(encoding="utf-8")

    def test_portfolio_is_a_first_class_tab(self):
        self.assertIn("{ id: 'portfolio', label: 'Portfolio'", self.html)
        self.assertIn("function PortfolioView", self.html)
        self.assertIn("useFetch('/api/portfolio')", self.html)

    def test_view_exposes_finish_and_evidence_decisions(self):
        for phrase in (
            "Finish gate",
            "Dominant gap",
            "Accepted outcome",
            "Evidence",
            "WIP limit",
            "Copy advice brief",
        ):
            self.assertIn(phrase, self.html)

    def test_advice_brief_is_copyable(self):
        self.assertIn("navigator.clipboard.writeText", self.html)
        self.assertIn("advice_brief", self.html)

    def test_dark_theme_is_the_default(self):
        self.assertIn('<html lang="en" data-theme="dark">', self.html)
        self.assertIn("JetBrains+Mono", self.html)


if __name__ == "__main__":
    unittest.main()
