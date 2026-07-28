#!/usr/bin/env python3
"""Deterministic CWC engagement-project portfolio scoring.

The manifest owns human-reviewed facts. This module validates and computes scores;
it never infers client adoption or commercial status from code activity.
"""

from __future__ import annotations

import copy
import json
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "accepted_outcome_adoption",
    "finishability",
    "commercial_commitment",
    "outcome_clarity",
    "blocker_ownership",
    "evidence_quality",
    "strategic_compounding",
)
ALLOWED_EVIDENCE_STATUSES = {"verified", "measured", "modelled", "unverified"}
DEFAULT_MANIFEST_PATH = Path(
    os.environ.get(
        "CWC_PORTFOLIO_MANIFEST",
        "/home/deploy/apps/cwc-control-plane/portfolio/projects.json",
    )
)


class PortfolioError(ValueError):
    """Raised when the portfolio manifest violates its contract."""


def _parse_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioError(f"{field} must be an ISO date") from exc


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Validate the score policy and all project records."""
    if manifest.get("version") != 2:
        raise PortfolioError("manifest version must be 2")

    policy = manifest.get("policy")
    if not isinstance(policy, dict):
        raise PortfolioError("policy is required")
    weights = policy.get("weights")
    if not isinstance(weights, dict):
        raise PortfolioError("policy.weights is required")
    if set(weights) != set(DIMENSIONS):
        missing = sorted(set(DIMENSIONS) - set(weights))
        extra = sorted(set(weights) - set(DIMENSIONS))
        raise PortfolioError(f"weights must use score dimensions; missing={missing}, extra={extra}")
    if any(not isinstance(weight, (int, float)) or weight <= 0 for weight in weights.values()):
        raise PortfolioError("weights must be positive numbers")
    if sum(weights.values()) != 100:
        raise PortfolioError("weights must sum to 100")

    projects = manifest.get("projects")
    if not isinstance(projects, list):
        raise PortfolioError("projects must be a list")

    seen_ids: set[str] = set()
    required = {
        "id",
        "client",
        "name",
        "repo",
        "owner",
        "lifecycle",
        "wip_class",
        "outcome_unit",
        "finish_gate",
        "evidence_requirement",
        "approval_boundary",
        "dimensions",
        "blockers",
        "evidence",
        "updated_at",
    }
    for project in projects:
        if not isinstance(project, dict):
            raise PortfolioError("each project must be an object")
        missing_fields = sorted(required - set(project))
        if missing_fields:
            raise PortfolioError(f"project missing required fields: {missing_fields}")
        project_id = project["id"]
        if not isinstance(project_id, str) or not project_id.strip():
            raise PortfolioError("project id must be a non-empty string")
        if project_id in seen_ids:
            raise PortfolioError(f"duplicate project id: {project_id}")
        seen_ids.add(project_id)

        for field in ("outcome_unit", "finish_gate"):
            value = project[field]
            if not isinstance(value, str) or not value.strip():
                raise PortfolioError(f"{project_id}: {field} must be a non-empty string")

        evidence_requirement = project["evidence_requirement"]
        if (
            not isinstance(evidence_requirement, list)
            or not evidence_requirement
            or any(
                not isinstance(requirement, str) or not requirement.strip()
                for requirement in evidence_requirement
            )
        ):
            raise PortfolioError(
                f"{project_id}: evidence_requirement must contain references"
            )
        approval_boundary = project["approval_boundary"]
        required_boundary = {
            "production",
            "spending",
            "client_communication",
            "acceptance_authority",
        }
        if (
            not isinstance(approval_boundary, dict)
            or set(approval_boundary) != required_boundary
        ):
            raise PortfolioError(f"{project_id}: approval_boundary is incomplete")
        for action in ("production", "spending", "client_communication"):
            if approval_boundary[action] != "required":
                raise PortfolioError(
                    f"{project_id}: {action} approval must remain required"
                )
        if (
            not isinstance(approval_boundary["acceptance_authority"], str)
            or not approval_boundary["acceptance_authority"].strip()
        ):
            raise PortfolioError(f"{project_id}: acceptance_authority is required")

        blockers = project["blockers"]
        if not isinstance(blockers, list):
            raise PortfolioError(f"{project_id}: blockers must be a list")
        for blocker in blockers:
            if not isinstance(blocker, dict):
                raise PortfolioError(f"{project_id}: blocker items must be objects")

        evidence_items = project["evidence"]
        if not isinstance(evidence_items, list):
            raise PortfolioError(f"{project_id}: evidence must be a list")

        dimensions = project["dimensions"]
        if not isinstance(dimensions, dict):
            raise PortfolioError(f"{project_id}: dimensions must be an object")
        missing_dimensions = sorted(set(DIMENSIONS) - set(dimensions))
        if missing_dimensions:
            raise PortfolioError(f"{project_id}: missing dimensions: {missing_dimensions}")
        extra_dimensions = sorted(set(dimensions) - set(DIMENSIONS))
        if extra_dimensions:
            raise PortfolioError(f"{project_id}: unknown dimensions: {extra_dimensions}")

        for dimension_name, dimension in dimensions.items():
            if not isinstance(dimension, dict):
                raise PortfolioError(f"{project_id}: {dimension_name} must be an object")
            rating = dimension.get("rating")
            if type(rating) is not int or not 0 <= rating <= 5:
                raise PortfolioError(f"{project_id}: {dimension_name} rating must be an integer between 0 and 5")
            if not str(dimension.get("rationale", "")).strip():
                raise PortfolioError(f"{project_id}: {dimension_name} rationale is required")
            evidence_refs = dimension.get("evidence_ids", [])
            if not isinstance(evidence_refs, list):
                raise PortfolioError(f"{project_id}: {dimension_name} evidence_ids must be a list")
            if any(not isinstance(evidence_id, str) or not evidence_id.strip() for evidence_id in evidence_refs):
                raise PortfolioError(f"{project_id}: {dimension_name} evidence_ids must be non-empty strings")

        evidence_ids: set[str] = set()
        for evidence in evidence_items:
            if not isinstance(evidence, dict):
                raise PortfolioError(f"{project_id}: evidence items must be objects")
            evidence_id = evidence.get("id")
            if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in evidence_ids:
                raise PortfolioError(f"{project_id}: evidence ids must be present and unique")
            evidence_ids.add(evidence_id)
            if evidence.get("status") not in ALLOWED_EVIDENCE_STATUSES:
                raise PortfolioError(f"{project_id}: invalid evidence status for {evidence_id}")
            _parse_date(evidence.get("observed_at"), f"{project_id}.{evidence_id}.observed_at")
            _parse_date(evidence.get("expires_at"), f"{project_id}.{evidence_id}.expires_at")

        referenced = {
            evidence_id
            for dimension in dimensions.values()
            for evidence_id in dimension.get("evidence_ids", [])
        }
        unknown = sorted(referenced - evidence_ids)
        if unknown:
            raise PortfolioError(f"{project_id}: unknown evidence ids: {unknown}")
        _parse_date(project["updated_at"], f"{project_id}.updated_at")


def _action_band(score: int) -> tuple[str, str]:
    if score >= 80:
        return "scale", "Scale after preserving reliability"
    if score >= 65:
        return "finish", "Finish now; protect capacity"
    if score >= 50:
        return "fix", "Fix adoption or the dominant blocker"
    if score >= 35:
        return "escalate", "Escalate, rescope, or set a kill date"
    return "pause", "Pause or archive unless the economics change"


def score_project(
    project: dict[str, Any],
    weights: dict[str, int | float],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Return a copy of one project enriched with score and evidence health."""
    as_of = as_of or date.today()
    result = copy.deepcopy(project)

    score = 0.0
    breakdown: list[dict[str, Any]] = []
    gaps: list[tuple[float, int, str]] = []
    for index, name in enumerate(DIMENSIONS):
        dimension = result["dimensions"][name]
        rating = float(dimension["rating"])
        weight = float(weights[name])
        points = rating / 5 * weight
        shortfall = (5 - rating) / 5 * weight
        score += points
        gaps.append((shortfall, -index, name))
        breakdown.append(
            {
                "id": name,
                "rating": dimension["rating"],
                "weight": weights[name],
                "points": round(points, 1),
                "rationale": dimension["rationale"],
                "evidence_ids": dimension.get("evidence_ids", []),
            }
        )

    rounded_score = int(round(score))
    action_band, recommendation = _action_band(rounded_score)
    status_counts = Counter(item["status"] for item in result.get("evidence", []))
    stale_ids: list[str] = []
    for item in result.get("evidence", []):
        expires_at = _parse_date(item.get("expires_at"), f"{result['id']}.{item.get('id')}.expires_at")
        item["freshness"] = "stale" if expires_at and expires_at < as_of else "current"
        if item["freshness"] == "stale":
            stale_ids.append(item["id"])

    warnings: list[str] = []
    if stale_ids:
        warnings.append("stale_evidence")
    if not result.get("evidence"):
        warnings.append("no_evidence")
    if result.get("lifecycle") == "blocked" and not result.get("blockers"):
        warnings.append("blocked_without_blocker_record")

    result.update(
        {
            "score": rounded_score,
            "action_band": action_band,
            "recommendation": recommendation,
            "dominant_gap": max(gaps)[2],
            "score_breakdown": breakdown,
            "evidence_summary": {
                "total": len(result.get("evidence", [])),
                "claim_counts": dict(sorted(status_counts.items())),
                "stale_count": len(stale_ids),
                "stale_ids": stale_ids,
            },
            "warnings": warnings,
            "scored_as_of": as_of.isoformat(),
        }
    )
    return result


def build_advice_brief(project: dict[str, Any]) -> str:
    """Create an inspectable prompt/context pack for human or Hermes review."""
    blockers = project.get("blockers", [])
    blocker_lines = [
        f"- {item.get('summary', 'Unknown blocker')} | owner: {item.get('owner', 'unassigned')} | "
        f"next: {item.get('next_action', 'undefined')} | decision: {item.get('decision_date', 'unset')}"
        for item in blockers
    ] or ["- None recorded"]
    counts = project.get("evidence_summary", {}).get("claim_counts", {})
    status_text = ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"
    return "\n".join(
        [
            "CWC PORTFOLIO DECISION BRIEF",
            f"Project: {project.get('client')} — {project.get('name')} ({project.get('id')})",
            f"Score: {project.get('score')}/100 | Recommendation: {project.get('recommendation')}",
            f"Dominant gap: {project.get('dominant_gap')}",
            f"Outcome unit: {project.get('outcome_unit')}",
            f"Finish gate: {project.get('finish_gate')}",
            "Blockers:",
            *blocker_lines,
            f"Evidence statuses: {status_text}; stale={project.get('evidence_summary', {}).get('stale_count', 0)}",
            "Question: What is the smallest decision or action that produces a paid, accepted outcome fastest?",
        ]
    )


def build_portfolio(manifest: dict[str, Any], *, as_of: date | None = None) -> dict[str, Any]:
    """Validate and return a ranked portfolio payload."""
    validate_manifest(manifest)
    as_of = as_of or date.today()
    policy = copy.deepcopy(manifest["policy"])
    projects = [score_project(item, policy["weights"], as_of=as_of) for item in manifest["projects"]]
    projects.sort(key=lambda item: -item["score"])
    for rank, item in enumerate(projects, start=1):
        item["rank"] = rank
        item["advice_brief"] = build_advice_brief(item)

    active_client = sum(
        1
        for item in projects
        if item.get("wip_class") == "client_outcome" and item.get("lifecycle") == "active"
    )
    active_strategic = sum(
        1
        for item in projects
        if item.get("wip_class") == "strategic_experiment" and item.get("lifecycle") == "active"
    )
    max_client = int(policy.get("max_client_outcomes", 2))
    max_strategic = int(policy.get("max_strategic_experiments", 1))
    return {
        "version": manifest["version"],
        "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        "scored_as_of": as_of.isoformat(),
        "policy": policy,
        "summary": {
            "total_projects": len(projects),
            "active_client_outcomes": active_client,
            "active_strategic_experiments": active_strategic,
            "wip_violation": active_client > max_client or active_strategic > max_strategic,
            "client_outcome_limit": max_client,
            "strategic_experiment_limit": max_strategic,
        },
        "projects": projects,
    }


def load_manifest(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    manifest_path = Path(path) if path else DEFAULT_MANIFEST_PATH
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise PortfolioError(f"portfolio manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise PortfolioError(f"portfolio manifest is invalid JSON: {manifest_path}") from exc
    validate_manifest(data)
    return data


def load_portfolio(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    return build_portfolio(load_manifest(path))


def get_project(portfolio: dict[str, Any], project_id: str) -> dict[str, Any] | None:
    return next((item for item in portfolio.get("projects", []) if item.get("id") == project_id), None)


def outcome_contract(project: dict[str, Any]) -> dict[str, Any]:
    """Project the canonical dispatch contract without scorecard or evidence contents."""
    return {
        "contract_version": 2,
        "project_id": project["id"],
        "outcome_unit": project["outcome_unit"],
        "finish_gate": project["finish_gate"],
        "evidence_requirement": copy.deepcopy(project["evidence_requirement"]),
        "approval_boundary": copy.deepcopy(project["approval_boundary"]),
    }


def outcome_contract_for_repo(
    repo: str, path: str | os.PathLike[str] | None = None
) -> dict[str, Any] | None:
    """Resolve one client-linked repository to its versioned outcome contract."""
    manifest = load_manifest(path)
    matches = [project for project in manifest["projects"] if project.get("repo") == repo]
    if not matches:
        return None
    if len(matches) > 1:
        raise PortfolioError(f"{repo}: multiple portfolio projects require explicit routing")
    return outcome_contract(matches[0])
