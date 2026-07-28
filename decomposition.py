#!/usr/bin/env python3
"""Validate, publish, and audit epic decompositions.

The module deliberately separates untrusted model output from GitHub mutation:
the complete plan must validate before a publisher is called. Invalid output is
accepted for one retry only; the next invalid submission moves the work item to
an explicit manual-review state.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


MAX_CHILDREN = 12
MAX_EVIDENCE_LENGTH = 240
REQUIRED_CHILD_FIELDS = (
    "id",
    "title",
    "scope",
    "acceptance_criteria",
    "size_days",
    "owner",
    "dependencies",
    "non_goals",
    "covers",
)
PLAN_FIELDS = {"schema_version", "parent_requirements", "children"}
REQUIREMENT_FIELDS = {"id", "description"}
CHILD_FIELDS = set(REQUIRED_CHILD_FIELDS)


class PlanValidationError(ValueError):
    """A plan failed deterministic structural or graph validation."""

    def __init__(self, errors: Iterable[dict[str, str]]):
        self.errors = sorted(
            list(errors), key=lambda error: (error["code"], error.get("path", ""))
        )
        self.codes = [error["code"] for error in self.errors]
        super().__init__("; ".join(self.codes))


class PublicationError(RuntimeError):
    """GitHub publication failed, with rollback evidence attached."""

    def __init__(
        self,
        message: str,
        *,
        created: list[dict[str, Any]] | None = None,
        rollback_complete: bool = True,
    ):
        super().__init__(message)
        self.created = created or []
        self.rollback_complete = rollback_complete


def _error(errors: list[dict[str, str]], code: str, path: str, message: str) -> None:
    errors.append({"code": code, "path": path, "message": message})


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _string_list(value: Any, *, minimum: int = 1, maximum: int | None = None) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= minimum
        and (maximum is None or len(value) <= maximum)
        and all(_nonempty_string(item) for item in value)
    )


def parse_plan(raw_plan: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Parse strict JSON or copy an already decoded object."""
    if isinstance(raw_plan, bytes):
        raw_plan = raw_plan.decode("utf-8")
    if isinstance(raw_plan, str):
        try:
            raw_plan = json.loads(
                raw_plan,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"Non-JSON constant {value} is forbidden.")
                ),
            )
        except json.JSONDecodeError as exc:
            raise PlanValidationError(
                [
                    {
                        "code": "output.invalid_json",
                        "path": "$",
                        "message": f"Strict JSON required (line {exc.lineno}, column {exc.colno}).",
                    }
                ]
            ) from None
        except ValueError as exc:
            raise PlanValidationError(
                [{"code": "output.invalid_json", "path": "$", "message": str(exc)}]
            ) from None
    if not isinstance(raw_plan, dict):
        raise PlanValidationError(
            [{"code": "output.not_object", "path": "$", "message": "Plan must be an object."}]
        )
    return json.loads(json.dumps(raw_plan))


def validate_plan(raw_plan: str | bytes | dict[str, Any]) -> dict[str, Any]:
    """Return a normalized, dependency-ordered plan or raise stable errors."""
    plan = parse_plan(raw_plan)
    errors: list[dict[str, str]] = []
    for field in sorted(set(plan) - PLAN_FIELDS):
        _error(
            errors,
            "schema.unknown_field",
            f"$.{field}",
            f"Unknown plan field: {field}.",
        )
    if plan.get("schema_version") != 1:
        _error(errors, "schema.version", "$.schema_version", "schema_version must equal 1.")

    requirements = plan.get("parent_requirements")
    requirement_ids: list[str] = []
    if not isinstance(requirements, list) or not requirements:
        _error(
            errors,
            "coverage.requirements_missing",
            "$.parent_requirements",
            "At least one parent requirement is required.",
        )
        requirements = []
    else:
        for index, requirement in enumerate(requirements):
            path = f"$.parent_requirements[{index}]"
            if not isinstance(requirement, dict):
                _error(errors, "coverage.requirement.invalid", path, "Requirement must be an object.")
                continue
            requirement_id = requirement.get("id")
            for field in sorted(set(requirement) - REQUIREMENT_FIELDS):
                _error(
                    errors,
                    "coverage.requirement.unknown_field",
                    f"{path}.{field}",
                    f"Unknown requirement field: {field}.",
                )
            if not _nonempty_string(requirement_id) or not _nonempty_string(
                requirement.get("description")
            ):
                _error(
                    errors,
                    "coverage.requirement.invalid",
                    path,
                    "Requirement needs non-empty id and description.",
                )
            else:
                requirement_ids.append(requirement_id)
        if len(requirement_ids) != len(set(requirement_ids)):
            _error(
                errors,
                "coverage.requirement.duplicate",
                "$.parent_requirements",
                "Requirement ids must be unique.",
            )

    children = plan.get("children")
    if not isinstance(children, list) or not children:
        _error(errors, "children.empty", "$.children", "At least one child is required.")
        children = []
    elif len(children) > MAX_CHILDREN:
        _error(
            errors,
            "children.too_many",
            "$.children",
            f"No more than {MAX_CHILDREN} children are allowed.",
        )

    child_ids: list[str] = []
    covers: set[str] = set()
    dependencies_by_id: dict[str, list[str]] = {}
    for index, child in enumerate(children):
        path = f"$.children[{index}]"
        if not isinstance(child, dict):
            _error(errors, "child.not_object", path, "Child must be an object.")
            continue
        for field in REQUIRED_CHILD_FIELDS:
            if field not in child:
                _error(
                    errors,
                    f"child.missing.{field}",
                    f"{path}.{field}",
                    f"{field} is required.",
                )
        for field in sorted(set(child) - CHILD_FIELDS):
            _error(
                errors,
                "child.unknown_field",
                f"{path}.{field}",
                f"Unknown child field: {field}.",
            )

        child_id = child.get("id")
        if not _nonempty_string(child_id) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", child_id):
            _error(
                errors,
                "child.id.invalid",
                f"{path}.id",
                "id must be a lowercase slug of at most 64 characters.",
            )
            continue
        child_ids.append(child_id)
        dependencies = child.get("dependencies")
        dependencies_by_id[child_id] = dependencies if isinstance(dependencies, list) else []

        for field in ("title", "scope", "owner"):
            if not _nonempty_string(child.get(field)):
                _error(errors, f"child.{field}.empty", f"{path}.{field}", f"{field} cannot be empty.")
        if _nonempty_string(child.get("scope")) and len(child["scope"].strip()) < 20:
            _error(
                errors,
                "child.scope.unbounded",
                f"{path}.scope",
                "scope must describe a bounded deliverable.",
            )
        if not _string_list(child.get("acceptance_criteria"), minimum=2, maximum=5):
            _error(
                errors,
                "child.acceptance_criteria.count",
                f"{path}.acceptance_criteria",
                "Provide 2-5 non-empty acceptance criteria.",
            )
        size_days = child.get("size_days")
        if isinstance(size_days, bool) or not isinstance(size_days, int) or not 1 <= size_days <= 3:
            _error(
                errors,
                "child.size_days.range",
                f"{path}.size_days",
                "size_days must be an integer from 1 through 3.",
            )
        if not _string_list(child.get("non_goals")):
            _error(
                errors,
                "child.non_goals.empty",
                f"{path}.non_goals",
                "At least one explicit non-goal is required.",
            )
        if not isinstance(dependencies, list) or not all(_nonempty_string(dep) for dep in dependencies):
            _error(
                errors,
                "child.dependencies.invalid",
                f"{path}.dependencies",
                "dependencies must be an array of child ids.",
            )
        child_covers = child.get("covers")
        if not _string_list(child_covers):
            _error(
                errors,
                "child.covers.empty",
                f"{path}.covers",
                "Each child must cover at least one parent requirement.",
            )
        else:
            covers.update(child_covers)

    if len(child_ids) != len(set(child_ids)):
        _error(errors, "child.id.duplicate", "$.children", "Child ids must be unique.")

    known_ids = set(child_ids)
    for child_id, dependencies in dependencies_by_id.items():
        for dependency in dependencies:
            if dependency not in known_ids:
                _error(
                    errors,
                    "dependencies.unknown",
                    f"$.children[{child_id}].dependencies",
                    f"Unknown dependency: {dependency}.",
                )
            elif dependency == child_id:
                _error(
                    errors,
                    "dependencies.circular",
                    f"$.children[{child_id}].dependencies",
                    "A child cannot depend on itself.",
                )

    unknown_coverage = covers - set(requirement_ids)
    if unknown_coverage:
        _error(
            errors,
            "coverage.unknown",
            "$.children",
            f"Unknown requirement ids: {', '.join(sorted(unknown_coverage))}.",
        )
    uncovered = set(requirement_ids) - covers
    if uncovered:
        _error(
            errors,
            "coverage.uncovered",
            "$.parent_requirements",
            f"Uncovered requirement ids: {', '.join(sorted(uncovered))}.",
        )

    ordered_ids: list[str] = []
    if len(child_ids) == len(set(child_ids)):
        remaining = list(child_ids)
        while remaining:
            ready = [
                child_id
                for child_id in remaining
                if all(
                    dependency not in known_ids or dependency in ordered_ids
                    for dependency in dependencies_by_id.get(child_id, [])
                )
            ]
            if not ready:
                _error(
                    errors,
                    "dependencies.circular",
                    "$.children",
                    "Dependency graph contains a cycle.",
                )
                break
            ordered_ids.extend(ready)
            remaining = [child_id for child_id in remaining if child_id not in ready]

    if errors:
        raise PlanValidationError(errors)
    child_by_id = {child["id"]: child for child in children}
    plan["children"] = [child_by_id[child_id] for child_id in ordered_ids]
    return plan


def _load_queue(queue_path: Path) -> dict[str, list[dict[str, Any]]]:
    try:
        queue = json.loads(queue_path.read_text())
    except FileNotFoundError:
        raise ValueError(f"Queue does not exist: {queue_path}") from None
    for key in ("pending", "completed", "failed"):
        if not isinstance(queue.get(key), list):
            raise ValueError(f"Queue field {key!r} must be a list.")
    return queue


def save_queue(queue_path: Path, queue: dict[str, Any]) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{queue_path.name}.", dir=queue_path.parent)
    try:
        with os.fdopen(fd, "w") as temporary:
            json.dump(queue, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, queue_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextmanager
def queue_lock(queue_path: Path):
    """Serialize short queue-file read/modify/write transactions."""
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = queue_path.with_name(f".{queue_path.name}.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _item_lock(queue_path: Path, item_id: str):
    """Serialize submissions for one parent without blocking unrelated enqueues."""
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    lock_path = queue_path.with_name(f".{queue_path.name}.{digest}.lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _find_pending(queue: dict[str, Any], item_id: str) -> tuple[int, dict[str, Any]]:
    for index, item in enumerate(queue["pending"]):
        if item.get("id") == item_id:
            return index, item
    raise ValueError(f"Pending decomposition not found: {item_id}")


def _terminal_result(queue: dict[str, Any], item_id: str) -> dict[str, Any] | None:
    for item in queue["completed"]:
        if item.get("id") == item_id:
            return {
                "status": "completed",
                "children": item.get("child_issues", []),
                "idempotent": True,
            }
    for item in queue["failed"]:
        if item.get("id") == item_id and item.get("manual_required"):
            result = {
                "status": "manual",
                "attempt": item.get(
                    "submission_attempt", item.get("validation_attempts", 0)
                ),
                "failure_class": item.get("failure_class", "validation_failed"),
                "idempotent": True,
            }
            if item.get("validation_errors"):
                result["errors"] = item["validation_errors"]
            if "rollback_complete" in item:
                result["rollback_complete"] = item["rollback_complete"]
            return result
    return None


def submit_plan(
    item_id: str,
    raw_plan: str | bytes | dict[str, Any],
    *,
    queue_path: str | Path,
    publisher: Callable[[dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Validate a plan, enforce retry policy, publish, and update its queue state."""
    queue_path = Path(queue_path)
    with _item_lock(queue_path, item_id):
        with queue_lock(queue_path):
            queue = _load_queue(queue_path)
            terminal = _terminal_result(queue, item_id)
            if terminal is not None:
                return terminal
            index, item = _find_pending(queue, item_id)
            try:
                plan = validate_plan(raw_plan)
            except PlanValidationError as exc:
                attempts = int(item.get("validation_attempts", 0)) + 1
                item["validation_attempts"] = attempts
                item["validation_errors"] = exc.errors
                item["last_validation_at"] = now().isoformat()
                if attempts == 1:
                    save_queue(queue_path, queue)
                    return {"status": "retry", "attempt": attempts, "errors": exc.errors}
                failed = queue["pending"].pop(index)
                failed.update(
                    {
                        "status": "manual",
                        "manual_required": True,
                        "failure_class": "validation_failed",
                        "failed_at": now().isoformat(),
                    }
                )
                queue["failed"].append(failed)
                save_queue(queue_path, queue)
                return {"status": "manual", "attempt": attempts, "errors": exc.errors}
            parent = copy_parent(item)

        try:
            created = publisher(parent, plan["children"])
        except PublicationError as exc:
            with queue_lock(queue_path):
                queue = _load_queue(queue_path)
                terminal = _terminal_result(queue, item_id)
                if terminal is not None:
                    return terminal
                index, item = _find_pending(queue, item_id)
                submission_attempt = int(item.get("validation_attempts", 0)) + 1
                failed = queue["pending"].pop(index)
                failed.update(
                    {
                        "status": "manual",
                        "manual_required": True,
                        "failure_class": "publication_failed",
                        "failure_message": sanitize_evidence(str(exc)),
                        "rollback_complete": exc.rollback_complete,
                        "created_child_issues": exc.created,
                        "submission_attempt": submission_attempt,
                        "failed_at": now().isoformat(),
                    }
                )
                queue["failed"].append(failed)
                save_queue(queue_path, queue)
                return {
                    "status": "manual",
                    "attempt": submission_attempt,
                    "failure_class": "publication_failed",
                    "rollback_complete": exc.rollback_complete,
                }

        with queue_lock(queue_path):
            queue = _load_queue(queue_path)
            terminal = _terminal_result(queue, item_id)
            if terminal is not None:
                return terminal
            index, _ = _find_pending(queue, item_id)
            completed = queue["pending"].pop(index)
            completed.update(
                {
                    "status": "completed",
                    "completed_at": now().isoformat(),
                    "validated_plan": plan,
                    "child_issues": created,
                }
            )
            queue["completed"].append(completed)
            save_queue(queue_path, queue)
            return {"status": "completed", "children": created}


def copy_parent(item: dict[str, Any]) -> dict[str, Any]:
    """Return only the parent fields a publisher needs."""
    return {
        key: item.get(key)
        for key in ("id", "repo", "issue_number", "title", "body")
    }


def _child_body(
    parent: dict[str, Any],
    child: dict[str, Any],
    created_by_id: dict[str, dict[str, Any]],
) -> str:
    dependencies = [
        f"#{created_by_id[dependency]['number']} ({dependency})"
        for dependency in child["dependencies"]
    ] or ["None"]
    return "\n".join(
        [
            _publication_marker(parent, child),
            "",
            f"Parent: #{parent['issue_number']}",
            "",
            "## Scope",
            child["scope"],
            "",
            "## Acceptance Criteria",
            *[f"- {criterion}" for criterion in child["acceptance_criteria"]],
            "",
            "## Size",
            f"{child['size_days']} day(s)",
            "",
            "## Owner",
            child["owner"],
            "",
            "## Dependencies",
            *[f"- {dependency}" for dependency in dependencies],
            "",
            "## Non-goals",
            *[f"- {non_goal}" for non_goal in child["non_goals"]],
        ]
    )


def _publication_marker(parent: dict[str, Any], child: dict[str, Any]) -> str:
    return (
        "<!-- cwc-decomposition:v1 "
        f"parent={parent['issue_number']} child={child['id']} -->"
    )


def _existing_published_children(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, dict[str, Any]]:
    result = runner(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "--method",
            "GET",
            f"repos/{parent['repo']}/issues",
            "-f",
            "state=all",
            "-f",
            "labels=epic-child",
            "-f",
            "per_page=100",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "gh issue lookup failed")
    try:
        pages = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("gh issue lookup returned invalid JSON") from None
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise RuntimeError("gh issue lookup returned an invalid result shape")

    open_issues = [
        issue
        for page in pages
        for issue in page
        if isinstance(issue, dict) and str(issue.get("state", "")).lower() == "open"
    ]
    existing: dict[str, dict[str, Any]] = {}
    for child in children:
        marker = _publication_marker(parent, child)
        matches = [
            issue for issue in open_issues if marker in str(issue.get("body") or "")
        ]
        if len(matches) > 1:
            raise RuntimeError(
                f"multiple open issues use decomposition marker for child {child['id']}"
            )
        if matches:
            issue = matches[0]
            number = issue.get("number")
            url = issue.get("html_url")
            if isinstance(number, bool) or not isinstance(number, int) or not _nonempty_string(url):
                raise RuntimeError("gh issue lookup returned incomplete issue metadata")
            existing[child["id"]] = {
                "id": child["id"],
                "number": number,
                "title": child["title"],
                "url": url,
                "reused": True,
            }
    return existing


def publish_to_github(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict[str, Any]]:
    """Publish validated children and close every created child on any failure."""
    created: list[dict[str, Any]] = []
    created_this_attempt: list[dict[str, Any]] = []
    created_by_id: dict[str, dict[str, Any]] = {}
    try:
        existing = _existing_published_children(parent, children, runner=runner)
        for child in children:
            if child["id"] in existing:
                created_issue = existing[child["id"]]
                created.append(created_issue)
                created_by_id[child["id"]] = created_issue
                continue
            result = runner(
                [
                    "gh",
                    "issue",
                    "create",
                    "--repo",
                    parent["repo"],
                    "--title",
                    f"[Parent #{parent['issue_number']}] {child['title']}",
                    "--body",
                    _child_body(parent, child, created_by_id),
                    "--label",
                    "epic-child",
                    "--assignee",
                    child["owner"],
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "gh issue create failed")
            match = re.search(r"/issues/(\d+)(?:\s*)$", result.stdout.strip())
            if not match:
                raise RuntimeError("gh issue create returned no issue number")
            created_issue = {
                "id": child["id"],
                "number": int(match.group(1)),
                "title": child["title"],
                "url": result.stdout.strip(),
                "reused": False,
            }
            created.append(created_issue)
            created_this_attempt.append(created_issue)
            created_by_id[child["id"]] = created_issue
        return created
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        rollback_complete = True
        for issue in reversed(created_this_attempt):
            try:
                rollback = runner(
                    [
                        "gh",
                        "issue",
                        "close",
                        str(issue["number"]),
                        "--repo",
                        parent["repo"],
                        "--reason",
                        "not planned",
                        "--comment",
                        "Closed automatically: validated decomposition publication did not complete.",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                rollback_complete = rollback_complete and rollback.returncode == 0
            except (OSError, subprocess.SubprocessError):
                rollback_complete = False
        raise PublicationError(
            str(exc), created=created_this_attempt, rollback_complete=rollback_complete
        ) from None


_SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"),
    re.compile(r"(?i)\b(?:token|secret|authorization|password)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
)


def sanitize_evidence(value: Any) -> str:
    text = str(value or "").replace("\n", " ").strip()[:MAX_EVIDENCE_LENGTH]
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def classify_failure(record: dict[str, Any]) -> tuple[str, str]:
    """Classify only supported evidence; absence or ambiguity stays unknown."""
    evidence = sanitize_evidence(record.get("error") or record.get("message") or "")
    lowered = evidence.lower()
    rules = (
        ("rate_limited", ("rate limit", "http 429", "too many requests")),
        ("auth_or_permission", ("http 401", "http 403", "permission denied", "forbidden")),
        ("invalid_output", ("invalid json", "malformed json", "schema", "validation")),
        ("timeout", ("timed out", "timeout")),
        ("github_publication", ("gh issue create", "github api", "publication")),
        ("duplicate_or_guarded", ("already decomposed", "duplicate", "guard")),
    )
    matches = [category for category, needles in rules if any(needle in lowered for needle in needles)]
    category = matches[0] if len(matches) == 1 else "unknown"
    return category, evidence


def build_rca_report(
    failed_records: list[dict[str, Any]], *, completed_count: int = 0
) -> dict[str, Any]:
    classified = []
    counts: Counter[str] = Counter()
    for index, record in enumerate(failed_records):
        category, evidence = classify_failure(record)
        counts[category] += 1
        classified.append(
            {
                "record": sanitize_evidence(record.get("id") or f"row-{index + 1}"),
                "category": category,
                "evidence": evidence or None,
            }
        )
    failed_count = len(failed_records)
    denominator = failed_count + max(completed_count, 0)
    return {
        "schema_version": 1,
        "failed_count": failed_count,
        "completed_count": max(completed_count, 0),
        "historical_failure_rate": failed_count / denominator if denominator else None,
        "categories": dict(sorted(counts.items())),
        "records": classified,
        "unknown_policy": "Missing or ambiguous evidence remains unknown.",
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone.")
    return parsed.astimezone(timezone.utc)


def build_production_observation(
    records: list[dict[str, Any]], *, deployed_at: str, as_of: str
) -> dict[str, Any]:
    deployed = _parse_datetime(deployed_at)
    observed = _parse_datetime(as_of)
    elapsed_days = max(0, (observed - deployed).days)
    if elapsed_days < 14:
        return {
            "status": "pending",
            "window_days": elapsed_days,
            "remaining_days": 14 - elapsed_days,
            "failed_count": None,
            "completed_count": None,
            "failure_rate": None,
            "target": 0.30,
        }
    window_end = deployed + timedelta(days=14)
    in_window = []
    for record in records:
        occurred_at = record.get("occurred_at")
        if not occurred_at:
            continue
        occurred = _parse_datetime(occurred_at)
        if deployed <= occurred < window_end:
            in_window.append(record)
    failed = sum(record.get("status") in {"failed", "manual"} for record in in_window)
    completed = sum(record.get("status") == "completed" for record in in_window)
    ignored = len(in_window) - failed - completed
    denominator = failed + completed
    return {
        "status": "observed",
        "window_days": 14,
        "remaining_days": 0,
        "failed_count": failed,
        "completed_count": completed,
        "ignored_count": ignored,
        "failure_rate": failed / denominator if denominator else None,
        "target": 0.30,
        "target_met": (failed / denominator < 0.30) if denominator else None,
    }


def audit_submission(
    item_id: str,
    result: dict[str, Any],
    *,
    logger: Callable[..., None],
) -> None:
    """Emit structured events after queue state has been durably written."""
    if result.get("idempotent"):
        return
    repo, separator, issue_text = item_id.rpartition("#")
    issue_number = int(issue_text) if separator and issue_text.isdigit() else None
    common = {"repo": repo or None, "source": "epic-decomposer-v2"}
    status = result.get("status")
    if status == "completed":
        for child in result.get("children", []):
            child_number = child.get("number")
            logger(
                "decompose.child_created",
                item_id=f"{repo}#{child_number}",
                repo=repo,
                issue_number=child_number,
                title=child.get("title"),
                details={"parent_id": item_id, "plan_child_id": child.get("id")},
                source=common["source"],
            )
        logger(
            "decompose.completed",
            item_id=item_id,
            repo=repo,
            issue_number=issue_number,
            details={"child_count": len(result.get("children", []))},
            source=common["source"],
        )
    elif status == "retry":
        logger(
            "decompose.validation_rejected",
            item_id=item_id,
            repo=repo,
            issue_number=issue_number,
            details={
                "attempt": result.get("attempt"),
                "error_codes": [error.get("code") for error in result.get("errors", [])],
            },
            source=common["source"],
        )
    elif status == "manual":
        logger(
            "decompose.failed",
            item_id=item_id,
            repo=repo,
            issue_number=issue_number,
            details={
                "manual_required": True,
                "attempt": result.get("attempt"),
                "failure_class": result.get("failure_class", "validation_failed"),
                "rollback_complete": result.get("rollback_complete"),
            },
            source=common["source"],
        )


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="Validate and normalize a plan.")
    validate_parser.add_argument("input", type=Path)
    submit_parser = subparsers.add_parser("submit", help="Validate and submit a queued plan.")
    submit_parser.add_argument("--item-id", required=True)
    submit_parser.add_argument("--input", required=True, type=Path)
    submit_parser.add_argument(
        "--queue",
        type=Path,
        default=Path(__file__).resolve().parent / "decompose-queue.json",
    )
    rca_parser = subparsers.add_parser("rca", help="Build a sanitized RCA report.")
    rca_parser.add_argument("input", type=Path)
    rca_parser.add_argument("--completed-count", type=int, default=0)
    observe_parser = subparsers.add_parser(
        "observe", help="Report the fixed two-week post-deploy failure rate."
    )
    observe_parser.add_argument("input", type=Path)
    observe_parser.add_argument("--deployed-at", required=True)
    observe_parser.add_argument("--as-of", required=True)

    args = parser.parse_args()
    try:
        if args.command == "validate":
            result = validate_plan(args.input.read_text())
        elif args.command == "submit":
            result = submit_plan(
                args.item_id,
                args.input.read_text(),
                queue_path=args.queue,
                publisher=publish_to_github,
            )
            try:
                from events import log_event

                audit_submission(args.item_id, result, logger=log_event)
            except Exception as exc:
                result["audit_warning"] = sanitize_evidence(exc)
        elif args.command == "rca":
            records = json.loads(args.input.read_text())
            if not isinstance(records, list):
                raise ValueError("RCA input must be a JSON array.")
            result = build_rca_report(records, completed_count=args.completed_count)
        else:
            records = json.loads(args.input.read_text())
            if not isinstance(records, list):
                raise ValueError("Observation input must be a JSON array.")
            result = build_production_observation(
                records, deployed_at=args.deployed_at, as_of=args.as_of
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") not in {"retry", "manual"} else 2
    except (OSError, ValueError, PlanValidationError) as exc:
        print(json.dumps({"status": "error", "error": sanitize_evidence(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
