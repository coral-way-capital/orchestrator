#!/usr/bin/env python3
"""Read-only accepted-outcome trace validation and funnel projection.

The acceptance envelope is deliberately independent from the PR ledger. This
module consumes references and provenance only; it has no acceptance mutation API.
"""

from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


STAGES = ("project", "issue", "run", "pr", "reviewed", "merged", "accepted")
STAGE_TRANSITIONS = (
    "project_to_issue",
    "issue_to_run",
    "run_to_pr",
    "pr_to_reviewed",
    "reviewed_to_merged",
    "merged_to_accepted",
)
ACCEPTANCE_STATUSES = {"unknown", "accepted", "rejected", "rework"}
FORBIDDEN_KEYS = {
    "body",
    "content",
    "raw",
    "raw_content",
    "email",
    "secret",
    "token",
    "password",
    "credential",
}
DEFAULT_TRACE_PATH = Path(
    os.environ.get(
        "CWC_OUTCOME_TRACES",
        str(Path.home() / ".hermes" / "issue-queue" / "outcome-traces.json"),
    )
)


class OutcomeTraceError(ValueError):
    """Raised when an outcome trace violates the references-only contract."""


def _timestamp(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise OutcomeTraceError(f"{field} must be an ISO timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeTraceError(f"{field} must be an ISO timestamp") from exc
    return value


def _reject_sensitive_keys(value: Any, path: str = "trace") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in FORBIDDEN_KEYS:
                raise OutcomeTraceError(
                    f"{path}.{key}: outcome evidence stores references only"
                )
            _reject_sensitive_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, f"{path}[{index}]")


def _required_reference(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OutcomeTraceError(f"{field} reference is required")


def validate_trace(trace: dict[str, Any]) -> None:
    _reject_sensitive_keys(trace)
    for field in ("trace_id", "client_slug"):
        _required_reference(trace.get(field), field)

    contract = trace.get("outcome_contract")
    if not isinstance(contract, dict) or contract.get("contract_version") != 1:
        raise OutcomeTraceError("outcome_contract version 1 is required")
    for field in ("project_id", "outcome_unit", "finish_gate"):
        _required_reference(contract.get(field), f"outcome_contract.{field}")
    requirements = contract.get("evidence_requirement")
    if not isinstance(requirements, list) or not requirements:
        raise OutcomeTraceError("outcome_contract.evidence_requirement is required")
    if not isinstance(contract.get("approval_boundary"), dict):
        raise OutcomeTraceError("outcome_contract.approval_boundary is required")

    stages = trace.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise OutcomeTraceError("stages must contain the canonical funnel")
    for stage_name in STAGES:
        stage = stages[stage_name]
        if not isinstance(stage, dict):
            raise OutcomeTraceError(f"stages.{stage_name} must be an object")
        _required_reference(stage.get("reference"), f"stages.{stage_name}")
        _timestamp(
            stage.get("occurred_at"),
            f"stages.{stage_name}.occurred_at",
            nullable=stage_name != "project",
        )
    seen_drop_off = False
    for stage_name in STAGES:
        occurred_at = stages[stage_name]["occurred_at"]
        if occurred_at is None:
            seen_drop_off = True
        elif seen_drop_off:
            raise OutcomeTraceError(
                f"stages.{stage_name} cannot occur after an earlier funnel drop-off"
            )

    acceptance = trace.get("acceptance")
    if not isinstance(acceptance, dict):
        raise OutcomeTraceError("acceptance envelope is required")
    status = acceptance.get("status")
    if status not in ACCEPTANCE_STATUSES:
        raise OutcomeTraceError("acceptance.status is invalid")
    provenance = acceptance.get("provenance")
    if not isinstance(provenance, dict):
        raise OutcomeTraceError("acceptance provenance is required")
    for field in ("system", "record_reference"):
        _required_reference(provenance.get(field), f"acceptance.provenance.{field}")
    _timestamp(provenance.get("recorded_at"), "acceptance.provenance.recorded_at")

    accepted_at = _timestamp(
        acceptance.get("accepted_at"), "acceptance.accepted_at", nullable=True
    )
    if status == "accepted":
        reviewer = acceptance.get("reviewer")
        if not isinstance(reviewer, dict):
            raise OutcomeTraceError("accepted outcome requires a reviewer reference")
        for field in ("reference", "role"):
            _required_reference(reviewer.get(field), f"acceptance.reviewer.{field}")
        evidence = acceptance.get("evidence_source")
        if not isinstance(evidence, dict):
            raise OutcomeTraceError("accepted outcome requires evidence source provenance")
        for field in ("source_type", "reference"):
            _required_reference(evidence.get(field), f"acceptance.evidence_source.{field}")
        observed_at = _timestamp(
            acceptance.get("observed_at"), "acceptance.observed_at"
        )
        if not accepted_at:
            raise OutcomeTraceError("accepted outcome requires accepted_at")
        if stages["accepted"]["occurred_at"] != accepted_at:
            raise OutcomeTraceError("accepted stage must use acceptance.accepted_at")
        if datetime.fromisoformat(accepted_at.replace("Z", "+00:00")) < datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        ):
            raise OutcomeTraceError("accepted_at cannot precede observed_at")
    else:
        if accepted_at is not None or stages["accepted"]["occurred_at"] is not None:
            raise OutcomeTraceError(
                "merge/review cannot set accepted without a separate accepted envelope"
            )


def _seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return int((last - first).total_seconds())


def build_funnel(payload: dict[str, Any], *, client_slug: str | None = None) -> dict[str, Any]:
    if payload.get("version") != 1 or not isinstance(payload.get("traces"), list):
        raise OutcomeTraceError("outcome trace payload version 1 is required")
    traces = copy.deepcopy(payload["traces"])
    for trace in traces:
        validate_trace(trace)
    if client_slug:
        traces = [trace for trace in traces if trace["client_slug"] == client_slug]

    for trace in traces:
        occurred = {
            name: trace["stages"][name]["occurred_at"] for name in STAGES
        }
        cycle_times = {}
        for transition, (start, end) in zip(
            STAGE_TRANSITIONS, zip(STAGES[:-1], STAGES[1:])
        ):
            duration = _seconds(occurred[start], occurred[end])
            if duration is not None and duration < 0:
                raise OutcomeTraceError(f"{transition} cannot be negative")
            cycle_times[transition] = duration
        trace["cycle_times_seconds"] = cycle_times

    counts = {
        stage: sum(trace["stages"][stage]["occurred_at"] is not None for trace in traces)
        for stage in STAGES
    }
    funnel = {}
    previous_count = None
    for stage in STAGES:
        count = counts[stage]
        funnel[stage] = {
            "count": count,
            "drop_off": None if previous_count is None else previous_count - count,
        }
        previous_count = count
    return {
        "version": 1,
        "read_only": True,
        "acceptance_inference": "forbidden",
        "funnel": funnel,
        "traces": traces,
    }


def load_funnel(
    path: str | os.PathLike[str] | None = None, *, client_slug: str | None = None
) -> dict[str, Any]:
    trace_path = Path(path) if path else DEFAULT_TRACE_PATH
    if not trace_path.exists():
        return {
            "version": 1,
            "read_only": True,
            "available": False,
            "acceptance_inference": "forbidden",
            "funnel": {
                stage: {"count": 0, "drop_off": None if index == 0 else 0}
                for index, stage in enumerate(STAGES)
            },
            "traces": [],
        }
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    result = build_funnel(payload, client_slug=client_slug)
    result["available"] = True
    result["source"] = str(trace_path)
    return result
