#!/usr/bin/env python3
"""Behavior tests for the CWC Portfolio Scorecard."""

import copy
import unittest
from datetime import date

from portfolio import (
    PortfolioError,
    build_advice_brief,
    build_portfolio,
    score_project,
    validate_manifest,
)


WEIGHTS = {
    "accepted_outcome_adoption": 25,
    "finishability": 20,
    "commercial_commitment": 15,
    "outcome_clarity": 15,
    "blocker_ownership": 10,
    "evidence_quality": 10,
    "strategic_compounding": 5,
}


def project(project_id="alpha", ratings=None, evidence=None, **overrides):
    ratings = ratings or {key: 3 for key in WEIGHTS}
    dimensions = {
        key: {
            "rating": ratings[key],
            "rationale": f"Rationale for {key}",
            "evidence_ids": [f"{project_id}-evidence"],
        }
        for key in WEIGHTS
    }
    base = {
        "id": project_id,
        "client": "Example Client",
        "name": "Example Project",
        "repo": "coral-way-capital/example",
        "owner": "Ivan",
        "lifecycle": "active",
        "wip_class": "client_outcome",
        "outcome_unit": "One accepted work unit",
        "finish_gate": "Client accepts the first work unit",
        "dimensions": dimensions,
        "blockers": [
            {
                "summary": "Client access is pending",
                "owner": "Client",
                "next_action": "Approve access",
                "decision_date": "2026-08-01",
            }
        ],
        "evidence": evidence
        or [
            {
                "id": f"{project_id}-evidence",
                "dimension": "accepted_outcome_adoption",
                "status": "verified",
                "source_type": "vault",
                "source": "01 - Clients/Example.md",
                "observed_at": "2026-07-27",
                "expires_at": "2026-08-10",
                "summary": "Current client status reviewed.",
            }
        ],
        "updated_at": "2026-07-27",
    }
    base.update(overrides)
    return base


def manifest(projects=None, weights=None):
    return {
        "version": 1,
        "policy": {
            "max_client_outcomes": 2,
            "max_strategic_experiments": 1,
            "weights": weights or WEIGHTS,
        },
        "projects": projects or [project()],
    }


class PortfolioScoreTests(unittest.TestCase):
    def test_perfect_ratings_score_one_hundred(self):
        ratings = {key: 5 for key in WEIGHTS}
        scored = score_project(project(ratings=ratings), WEIGHTS, as_of=date(2026, 7, 27))
        self.assertEqual(scored["score"], 100)
        self.assertEqual(scored["action_band"], "scale")

    def test_weighted_score_rewards_adoption_and_finishability(self):
        ratings = {key: 0 for key in WEIGHTS}
        ratings["accepted_outcome_adoption"] = 5
        ratings["finishability"] = 5
        scored = score_project(project(ratings=ratings), WEIGHTS, as_of=date(2026, 7, 27))
        self.assertEqual(scored["score"], 45)
        self.assertEqual(scored["action_band"], "escalate")

    def test_dominant_gap_prefers_highest_weighted_shortfall(self):
        ratings = {key: 5 for key in WEIGHTS}
        ratings["accepted_outcome_adoption"] = 1
        ratings["strategic_compounding"] = 0
        scored = score_project(project(ratings=ratings), WEIGHTS, as_of=date(2026, 7, 27))
        self.assertEqual(scored["dominant_gap"], "accepted_outcome_adoption")

    def test_expired_evidence_is_reported_as_stale(self):
        stale = project()["evidence"]
        stale[0]["expires_at"] = "2026-07-26"
        scored = score_project(project(evidence=stale), WEIGHTS, as_of=date(2026, 7, 27))
        self.assertEqual(scored["evidence_summary"]["stale_count"], 1)
        self.assertIn("stale_evidence", scored["warnings"])

    def test_portfolio_is_ranked_deterministically(self):
        low = project("low", ratings={key: 1 for key in WEIGHTS})
        high = project("high", ratings={key: 4 for key in WEIGHTS})
        result = build_portfolio(manifest([low, high]), as_of=date(2026, 7, 27))
        self.assertEqual([p["id"] for p in result["projects"]], ["high", "low"])
        self.assertEqual(result["projects"][0]["rank"], 1)

    def test_equal_score_ties_preserve_manifest_order(self):
        first = project("zeta")
        second = project("alpha")
        result = build_portfolio(manifest([first, second]), as_of=date(2026, 7, 27))
        self.assertEqual([p["id"] for p in result["projects"]], ["zeta", "alpha"])

    def test_active_client_outcomes_over_limit_create_wip_violation(self):
        projects = [project("one"), project("two"), project("three")]
        result = build_portfolio(manifest(projects), as_of=date(2026, 7, 27))
        self.assertTrue(result["summary"]["wip_violation"])
        self.assertEqual(result["summary"]["active_client_outcomes"], 3)

    def test_advice_brief_contains_decision_context_not_secrets(self):
        scored = score_project(project(), WEIGHTS, as_of=date(2026, 7, 27))
        brief = build_advice_brief(scored)
        self.assertIn("Example Project", brief)
        self.assertIn("Finish gate", brief)
        self.assertIn("Client access is pending", brief)
        self.assertIn("Evidence statuses", brief)


class PortfolioValidationTests(unittest.TestCase):
    def test_weights_must_total_one_hundred(self):
        bad_weights = copy.deepcopy(WEIGHTS)
        bad_weights["strategic_compounding"] = 4
        with self.assertRaisesRegex(PortfolioError, "sum to 100"):
            validate_manifest(manifest(weights=bad_weights))

    def test_duplicate_project_ids_are_rejected(self):
        with self.assertRaisesRegex(PortfolioError, "duplicate project id"):
            validate_manifest(manifest([project("same"), project("same")]))

    def test_all_dimensions_are_required(self):
        incomplete = project()
        del incomplete["dimensions"]["finishability"]
        with self.assertRaisesRegex(PortfolioError, "missing dimensions"):
            validate_manifest(manifest([incomplete]))

    def test_ratings_must_be_between_zero_and_five(self):
        invalid = project()
        invalid["dimensions"]["finishability"]["rating"] = 6
        with self.assertRaisesRegex(PortfolioError, "integer between 0 and 5"):
            validate_manifest(manifest([invalid]))

    def test_ratings_must_be_integers(self):
        invalid = project()
        invalid["dimensions"]["finishability"]["rating"] = 2.5
        with self.assertRaisesRegex(PortfolioError, "integer between 0 and 5"):
            validate_manifest(manifest([invalid]))

        invalid_bool = project()
        invalid_bool["dimensions"]["finishability"]["rating"] = True
        with self.assertRaisesRegex(PortfolioError, "integer between 0 and 5"):
            validate_manifest(manifest([invalid_bool]))

    def test_evidence_and_blockers_must_be_lists(self):
        invalid_evidence = project()
        invalid_evidence["evidence"] = None
        with self.assertRaisesRegex(PortfolioError, "evidence must be a list"):
            validate_manifest(manifest([invalid_evidence]))

        invalid_blockers = project()
        invalid_blockers["blockers"] = None
        with self.assertRaisesRegex(PortfolioError, "blockers must be a list"):
            validate_manifest(manifest([invalid_blockers]))


if __name__ == "__main__":
    unittest.main()
