#!/usr/bin/env python3
"""Read-only accepted-outcome trace validation and funnel projection.

The acceptance envelope is deliberately independent from the PR ledger. This
module consumes references and provenance only; it has no acceptance mutation API.
"""

from __future__ import annotations

import copy
import json
import os
import re
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
EVIDENCE_SOURCE_TYPES = {
    "client_confirmation",
    "generated_report",
    "telemetry",
    "vault",
    "contract",
}
MAX_TRACES = 1_000
MAX_TRACE_FILE_BYTES = 1_048_576
MAX_REFERENCE_LENGTH = 512
MAX_TEXT_LENGTH = 2_048
FORBIDDEN_KEYS = {
    "body",
    "content",
    "raw",
    "raw_content",
    "email",
    "message",
    "payload",
    "private_key",
    "secret",
    "token",
    "password",
    "credential",
}
TRACE_FIELDS = {"trace_id", "client_slug", "project_contract", "stages", "acceptance"}
PROJECT_CONTRACT_FIELDS = {"version", "project_id"}
STAGE_FIELDS = {"reference", "occurred_at"}
ACCEPTANCE_FIELDS = {
    "reviewer",
    "status",
    "evidence_source",
    "observed_at",
    "accepted_at",
    "provenance",
}
REVIEWER_FIELDS = {"reference", "role"}
EVIDENCE_SOURCE_FIELDS = {"source_type", "reference"}
PROVENANCE_FIELDS = {"system", "recorded_at", "record_reference"}
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
TRACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
REFERENCE_RE = re.compile(
    r"^(?:https://github\.com/[A-Za-z0-9._/-]+|"
    r"(?:github|vault|report|telemetry|synthetic)://"
    r"[A-Za-z0-9][A-Za-z0-9._/-]*)$"
)
RFC3339_RE = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"
)
SYSTEM_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
LOCAL_PATH_RE = re.compile(
    r"(?:^|[\s:=])(?:/home/|/var/|/tmp/|~/|file://|[A-Za-z]:\\)",
    re.IGNORECASE,
)
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
    if not isinstance(value, str) or not RFC3339_RE.fullmatch(value):
        raise OutcomeTraceError(f"{field} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OutcomeTraceError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.utcoffset() is None:
        raise OutcomeTraceError(f"{field} must include a timezone")
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
    elif isinstance(value, str) and LOCAL_PATH_RE.search(value):
        raise OutcomeTraceError(f"{path}: local paths are not permitted")


def _exact_keys(value: dict[str, Any], expected: set[str], field: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        extra = sorted(set(value) - expected)
        raise OutcomeTraceError(f"{field} fields are non-canonical; missing={missing}, extra={extra}")


def _required_text(
    value: Any, field: str, *, maximum: int = MAX_TEXT_LENGTH
) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise OutcomeTraceError(f"{field} must be a bounded non-empty string")
    return value


def _required_reference(value: Any, field: str) -> str:
    reference = _required_text(value, field, maximum=MAX_REFERENCE_LENGTH)
    if not REFERENCE_RE.fullmatch(reference):
        raise OutcomeTraceError(f"{field} reference must use an allowlisted scheme")
    return reference


def validate_client_slug(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise OutcomeTraceError("client filter must be a lowercase slug of at most 63 characters")
    return value


def _validate_nullable_reviewer(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OutcomeTraceError("acceptance.reviewer must be an object or null")
    _exact_keys(value, REVIEWER_FIELDS, "acceptance.reviewer")
    _required_reference(value["reference"], "acceptance.reviewer.reference")
    _required_text(value["role"], "acceptance.reviewer.role", maximum=100)


def _validate_nullable_evidence_source(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OutcomeTraceError("acceptance.evidence_source must be an object or null")
    _exact_keys(value, EVIDENCE_SOURCE_FIELDS, "acceptance.evidence_source")
    if value["source_type"] not in EVIDENCE_SOURCE_TYPES:
        raise OutcomeTraceError("acceptance.evidence_source.source_type is invalid")
    _required_reference(
        value["reference"], "acceptance.evidence_source.reference"
    )


def _validate_nullable_provenance(value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise OutcomeTraceError("acceptance.provenance must be an object or null")
    _exact_keys(value, PROVENANCE_FIELDS, "acceptance.provenance")
    system = _required_text(value["system"], "acceptance.provenance.system")
    if not SYSTEM_RE.fullmatch(system):
        raise OutcomeTraceError("acceptance.provenance.system is invalid")
    _timestamp(value["recorded_at"], "acceptance.provenance.recorded_at")
    _required_reference(
        value["record_reference"], "acceptance.provenance.record_reference"
    )


def validate_trace(
    trace: dict[str, Any],
    *,
    projects_by_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    if not isinstance(trace, dict):
        raise OutcomeTraceError("each trace must be an object")
    _reject_sensitive_keys(trace)
    _exact_keys(trace, TRACE_FIELDS, "trace")
    trace_id = _required_text(trace["trace_id"], "trace_id", maximum=128)
    if not TRACE_ID_RE.fullmatch(trace_id):
        raise OutcomeTraceError("trace_id must be a lowercase slug")
    validate_client_slug(trace["client_slug"])

    project_contract = trace.get("project_contract")
    if not isinstance(project_contract, dict):
        raise OutcomeTraceError("project_contract reference is required")
    _exact_keys(project_contract, PROJECT_CONTRACT_FIELDS, "project_contract")
    if project_contract.get("version") != 2:
        raise OutcomeTraceError("project_contract version 2 is required")
    project_id = project_contract.get("project_id")
    if not isinstance(project_id, str) or not IDENTIFIER_RE.fullmatch(project_id):
        raise OutcomeTraceError("project_contract.project_id must be a lowercase slug")
    if trace["client_slug"] != project_id.split("-", 1)[0]:
        raise OutcomeTraceError("client_slug does not match project_contract.project_id")
    project = None
    if projects_by_id is not None:
        project = projects_by_id.get(project_id)
        if project is None:
            raise OutcomeTraceError(f"unknown project_contract.project_id: {project_id}")

    stages = trace.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        raise OutcomeTraceError("stages must contain the canonical funnel")
    for stage_name in STAGES:
        stage = stages[stage_name]
        if not isinstance(stage, dict):
            raise OutcomeTraceError(f"stages.{stage_name} must be an object")
        _exact_keys(stage, STAGE_FIELDS, f"stages.{stage_name}")
        reference = stage.get("reference")
        occurred_at = stage.get("occurred_at")
        if (reference is None) != (occurred_at is None):
            raise OutcomeTraceError(
                f"stages.{stage_name} reference and occurred_at must both be set or null"
            )
        if reference is not None:
            _required_reference(reference, f"stages.{stage_name}.reference")
        _timestamp(
            occurred_at, f"stages.{stage_name}.occurred_at", nullable=True
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
    _exact_keys(acceptance, ACCEPTANCE_FIELDS, "acceptance")
    status = acceptance.get("status")
    if status not in ACCEPTANCE_STATUSES:
        raise OutcomeTraceError("acceptance.status is invalid")
    reviewer = acceptance.get("reviewer")
    evidence = acceptance.get("evidence_source")
    provenance = acceptance.get("provenance")
    _validate_nullable_reviewer(reviewer)
    _validate_nullable_evidence_source(evidence)
    _validate_nullable_provenance(provenance)
    observed_at = _timestamp(
        acceptance.get("observed_at"), "acceptance.observed_at", nullable=True
    )
    accepted_at = _timestamp(
        acceptance.get("accepted_at"), "acceptance.accepted_at", nullable=True
    )
    if status == "accepted":
        if not isinstance(reviewer, dict):
            raise OutcomeTraceError("accepted outcome requires a reviewer reference")
        if not isinstance(evidence, dict):
            raise OutcomeTraceError("accepted outcome requires evidence source provenance")
        if not isinstance(provenance, dict):
            raise OutcomeTraceError("accepted outcome requires provenance")
        if not observed_at:
            raise OutcomeTraceError("accepted outcome requires observed_at")
        if not accepted_at:
            raise OutcomeTraceError("accepted outcome requires accepted_at")
        if stages["accepted"]["occurred_at"] != accepted_at:
            raise OutcomeTraceError("accepted stage must use acceptance.accepted_at")
        if project is not None and reviewer["role"] != (
            project.get("approval_boundary") or {}
        ).get("acceptance_authority"):
            raise OutcomeTraceError("reviewer is outside the project acceptance authority")
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        accepted = datetime.fromisoformat(accepted_at.replace("Z", "+00:00"))
        recorded = datetime.fromisoformat(
            provenance["recorded_at"].replace("Z", "+00:00")
        )
        if not observed <= accepted <= recorded:
            raise OutcomeTraceError(
                "acceptance timestamps must satisfy observed_at <= accepted_at <= recorded_at"
            )
    elif status in {"rejected", "rework"}:
        if not all(
            (
                isinstance(reviewer, dict),
                isinstance(evidence, dict),
                bool(observed_at),
                isinstance(provenance, dict),
            )
        ):
            raise OutcomeTraceError(
                f"{status} outcome requires reviewer, evidence, observation, and provenance"
            )
        if project is not None and reviewer["role"] != (
            project.get("approval_boundary") or {}
        ).get("acceptance_authority"):
            raise OutcomeTraceError("reviewer is outside the project acceptance authority")
        if accepted_at is not None or stages["accepted"]["occurred_at"] is not None:
            raise OutcomeTraceError("non-accepted outcome cannot populate accepted_at")
    else:
        if any(
            value is not None
            for value in (
                reviewer,
                evidence,
                observed_at,
                accepted_at,
                provenance,
                stages["accepted"]["reference"],
                stages["accepted"]["occurred_at"],
            )
        ):
            raise OutcomeTraceError(
                "unknown acceptance must remain entirely null"
            )


def _seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return int((last - first).total_seconds())


def build_funnel(
    payload: dict[str, Any],
    *,
    client_slug: str | None = None,
    projects_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"version", "traces"}:
        raise OutcomeTraceError("outcome trace payload fields are non-canonical")
    if payload.get("version") != 1 or not isinstance(payload.get("traces"), list):
        raise OutcomeTraceError("outcome trace payload version 1 is required")
    if not payload["traces"]:
        raise OutcomeTraceError("outcome trace payload requires at least one trace")
    if len(payload["traces"]) > MAX_TRACES:
        raise OutcomeTraceError(f"outcome trace payload exceeds {MAX_TRACES} traces")
    traces = copy.deepcopy(payload["traces"])
    trace_ids: set[str] = set()
    for trace in traces:
        validate_trace(trace, projects_by_id=projects_by_id)
        if trace["trace_id"] in trace_ids:
            raise OutcomeTraceError(f"duplicate trace_id: {trace['trace_id']}")
        trace_ids.add(trace["trace_id"])
    if client_slug is not None:
        validate_client_slug(client_slug)
        traces = [trace for trace in traces if trace["client_slug"] == client_slug]
    traces.sort(key=lambda trace: trace["trace_id"])

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
    path: str | os.PathLike[str] | None = None,
    *,
    client_slug: str | None = None,
    projects_by_id: dict[str, dict[str, Any]] | None = None,
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
    if trace_path.stat().st_size > MAX_TRACE_FILE_BYTES:
        raise OutcomeTraceError(
            f"outcome trace file exceeds {MAX_TRACE_FILE_BYTES} bytes"
        )
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    result = build_funnel(
        payload, client_slug=client_slug, projects_by_id=projects_by_id
    )
    result["available"] = True
    return result
