#!/usr/bin/env python3
"""Deterministic tests for the read-only repository-context report adapter."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import context_audit_reports


SUBSCORES = {
    "instructions": 100,
    "duplication": 100,
    "contradictions": 100,
    "references": 100,
    "discoverability": 50,
    "ci": 0,
    "ownership": 100,
}
WEIGHTS = {
    "instructions": 20,
    "duplication": 10,
    "contradictions": 15,
    "references": 10,
    "discoverability": 20,
    "ci": 15,
    "ownership": 10,
}
INVENTORY_REVISION = "a" * 40
REPOSITORY_REVISION = "b" * 40


def evidence(repository="orchestrator", path="AGENTS.md", line=3):
    return {
        "category": "instructions",
        "message": "redacted synthetic observation",
        "path": path,
        "line": line,
        "url": (
            f"https://github.com/coral-way-capital/{repository}/blob/"
            f"{REPOSITORY_REVISION}/{path}#L{line}"
        ),
    }


def report(kind="delta", observed_at="2026-07-27"):
    repository_evidence = evidence()
    result = {
        "schema_version": 1,
        "kind": kind,
        "observed_at": observed_at,
        "inventory": "repository-context/repositories.json",
        "weights": WEIGHTS,
        "repository_count": 2,
        "repositories": [
            {
                "name": "coral-way-capital/orchestrator",
                "default_branch": "main",
                "audit_ref": REPOSITORY_REVISION,
                "score": 55,
                "baseline_score": 72 if kind == "delta" else None,
                "change": -17 if kind == "delta" else None,
                "subscores": SUBSCORES,
                "evidence": [repository_evidence],
            },
            {
                "name": "coral-way-capital/cwc-control-plane",
                "default_branch": "main",
                "audit_ref": "c" * 40,
                "score": 85,
                "baseline_score": 80 if kind == "delta" else None,
                "change": 5 if kind == "delta" else None,
                "subscores": {**SUBSCORES, "ci": 100},
                "evidence": [
                    {
                        **evidence("cwc-control-plane", "docs/context.md", 9),
                        "url": (
                            "https://github.com/coral-way-capital/cwc-control-plane/blob/"
                            f"{'c' * 40}/docs/context.md#L9"
                        ),
                    }
                ],
            },
        ],
        "findings": [
            {
                "id": "orchestrator--below-60",
                "repository": "coral-way-capital/orchestrator",
                "reason": "score_below_60",
                "score": 55,
                "baseline_score": 72 if kind == "delta" else None,
                "evidence": [repository_evidence],
            },
            {
                "id": "orchestrator--drop",
                "repository": "coral-way-capital/orchestrator",
                "reason": "score_drop",
                "score": 55,
                "baseline_score": 72,
                "evidence": [repository_evidence],
            },
        ],
        "actions": [],
        "synthetic_secret": "must-not-cross-the-adapter",
    }
    if kind == "delta":
        result["baseline"] = "repository-context/baselines/2026-07-20.json"
    return result


def write_report_root(root: Path, *, observed_at="2026-07-27"):
    (root / "baselines").mkdir()
    (root / "deltas").mkdir()
    inventory = {
        "version": 1,
        "observed_at": observed_at,
        "repositories": [
            {
                "name": "coral-way-capital/orchestrator",
                "default_branch": "main",
                "audit_ref": REPOSITORY_REVISION,
                "owner": "coral-way-capital",
                "lifecycle": "active",
                "production_status": "production",
                "stack": ["Python"],
                "agent_instructions": ["AGENTS.md"],
                "ci_workflows": [],
                "verification_command": "python3 -m unittest",
            },
            {
                "name": "coral-way-capital/cwc-control-plane",
                "default_branch": "main",
                "audit_ref": "c" * 40,
                "owner": "coral-way-capital",
                "lifecycle": "active",
                "production_status": "non-production",
                "stack": ["Python"],
                "agent_instructions": ["AGENTS.md"],
                "ci_workflows": [],
                "verification_command": "python3 scripts/verify_control_plane.py",
            },
        ],
        "evidence_source": {
            "repository": "coral-way-capital/cwc-control-plane",
            "ref": INVENTORY_REVISION,
            "path": "repository-context/repositories.json",
        },
    }
    (root / "repositories.json").write_text(json.dumps(inventory), encoding="utf-8")
    (root / "baselines" / "2026-07-20.json").write_text(
        json.dumps(report("baseline", "2026-07-20")), encoding="utf-8"
    )
    (root / "deltas" / "2026-07-27-weekly.json").write_text(
        json.dumps(report("delta", observed_at)), encoding="utf-8"
    )


class ContextAuditReportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        write_report_root(self.root)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_latest_delta_is_normalized_without_contents_or_local_paths(self):
        payload = context_audit_reports.load_context_audit(
            self.root, today=date(2026, 7, 27)
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["report_kind"], "delta")
        self.assertEqual(payload["observed_at"], "2026-07-27")
        self.assertEqual(payload["inventory_revision"], INVENTORY_REVISION)
        self.assertEqual(payload["summary"]["coverage_percent"], 50)
        self.assertEqual(payload["summary"]["threshold_findings"], 2)
        self.assertEqual(payload["delta"]["largest_drop"], -17)
        self.assertFalse(payload["stale"])
        self.assertEqual(payload["repositories"][0]["change"], -17)
        self.assertEqual(
            set(payload["repositories"][0]["evidence"][0]),
            {"label", "path", "line", "url"},
        )
        serialized = json.dumps(payload)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("redacted synthetic observation", serialized)
        self.assertNotIn("must-not-cross-the-adapter", serialized)
        self.assertNotIn('"actions"', serialized)
        self.assertNotIn('"inventory"', serialized)
        self.assertNotIn('"baseline"', serialized)

    def test_report_is_honestly_marked_stale(self):
        payload = context_audit_reports.load_context_audit(
            self.root, today=date(2026, 9, 30), stale_after_days=30
        )
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["age_days"], 65)

    def test_unavailable_root_returns_safe_payload(self):
        payload = context_audit_reports.load_context_audit(self.root / "missing")
        self.assertEqual(
            payload,
            {
                "available": False,
                "error": "repository context audit unavailable",
                "reason": "report root unavailable",
                "repositories": [],
                "findings": [],
            },
        )

    def test_rejects_symlinked_report_escape(self):
        outside = self.root.parent / f"{self.root.name}-outside.json"
        outside.write_text(json.dumps(report("delta")), encoding="utf-8")
        report_path = self.root / "deltas" / "2026-07-27-weekly.json"
        report_path.unlink()
        report_path.symlink_to(outside)
        self.addCleanup(outside.unlink)

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "no valid canonical reports")

    def test_rejects_noncanonical_configured_root(self):
        traversing_root = self.root / ".." / self.root.name
        self.assertFalse(
            context_audit_reports.load_context_audit(traversing_root)["available"]
        )

        with tempfile.TemporaryDirectory() as alias_tempdir:
            alias = Path(alias_tempdir) / "alias"
            alias.symlink_to(self.root.parent, target_is_directory=True)
            ancestor_symlink_root = alias / self.root.name
            self.assertFalse(
                context_audit_reports.load_context_audit(ancestor_symlink_root)[
                    "available"
                ]
            )

    def test_rejects_report_revision_not_pinned_by_inventory(self):
        unsafe = report("delta")
        unsafe["repositories"][0]["audit_ref"] = "d" * 40
        unsafe["repositories"][0]["evidence"][0]["url"] = (
            "https://github.com/coral-way-capital/orchestrator/blob/"
            f"{'d' * 40}/AGENTS.md#L3"
        )
        (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
            json.dumps(unsafe), encoding="utf-8"
        )

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])

    def test_rejects_cross_repository_revision_allowlist_pair(self):
        unsafe = report("delta")
        unsafe["repositories"][0]["evidence"][0]["url"] = (
            "https://github.com/coral-way-capital/cwc-control-plane/blob/"
            f"{'c' * 40}/AGENTS.md#L3"
        )
        (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
            json.dumps(unsafe), encoding="utf-8"
        )

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])

    def test_rejects_reports_missing_schema_required_fields(self):
        for field in ("default_branch", "category", "message"):
            with self.subTest(field=field):
                unsafe = report("delta")
                if field == "default_branch":
                    del unsafe["repositories"][0][field]
                else:
                    del unsafe["repositories"][0]["evidence"][0][field]
                (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
                    json.dumps(unsafe), encoding="utf-8"
                )

                payload = context_audit_reports.load_context_audit(self.root)
                self.assertFalse(payload["available"])

    def test_rejects_inconsistent_threshold_finding(self):
        unsafe = report("delta")
        unsafe["findings"][0]["score"] = 99
        (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
            json.dumps(unsafe), encoding="utf-8"
        )

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])

    def test_rejects_noncanonical_and_traversal_references(self):
        unsafe = report("delta")
        unsafe["inventory"] = "../secrets.json"
        (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
            json.dumps(unsafe), encoding="utf-8"
        )
        (self.root / "deltas" / "latest.json").write_text(
            json.dumps(report("delta")), encoding="utf-8"
        )

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "no valid canonical reports")

    def test_rejects_evidence_url_that_does_not_match_audited_revision(self):
        unsafe = report("delta")
        unsafe["repositories"][0]["evidence"][0]["url"] = (
            "https://example.com/secret?token=do-not-return"
        )
        (self.root / "deltas" / "2026-07-27-weekly.json").write_text(
            json.dumps(unsafe), encoding="utf-8"
        )

        payload = context_audit_reports.load_context_audit(self.root)
        self.assertFalse(payload["available"])
        self.assertNotIn("token", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
