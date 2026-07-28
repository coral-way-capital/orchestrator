#!/usr/bin/env python3
"""Safe, read-only adapter for versioned repository-context audit reports."""

from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any


SUBSCORES = (
    "instructions",
    "duplication",
    "contradictions",
    "references",
    "discoverability",
    "ci",
    "ownership",
)
REPORT_PATTERNS = {
    "baseline": re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\.json$"),
    "delta": re.compile(
        r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-[a-z0-9][a-z0-9-]*)?\.json$"
    ),
}
REPOSITORY_RE = re.compile(r"^coral-way-capital/[A-Za-z0-9._-]+$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
EVIDENCE_URL_RE = re.compile(
    r"^https://github\.com/(?P<repository>coral-way-capital/[A-Za-z0-9._-]+)/"
    r"blob/(?P<revision>[0-9a-f]{40})/(?P<path>[^?#]+)#L(?P<line>[1-9][0-9]*)$"
)


def _configured_stale_days() -> int:
    try:
        value = int(os.environ.get("CWC_CONTEXT_AUDIT_STALE_DAYS", "30"))
    except ValueError:
        return 30
    return value if value >= 0 else 30


DEFAULT_REPORT_ROOT = Path(
    os.environ.get(
        "CWC_CONTEXT_AUDIT_REPORT_ROOT",
        "/home/deploy/apps/cwc-control-plane/repository-context",
    )
)
DEFAULT_STALE_AFTER_DAYS = _configured_stale_days()


class ContextAuditReportError(ValueError):
    """Raised internally when a versioned report violates the display contract."""


def _unavailable(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "error": "repository context audit unavailable",
        "reason": reason,
        "repositories": [],
        "findings": [],
    }


def _safe_relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ContextAuditReportError("invalid evidence path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or any(not part for part in path.parts):
        raise ContextAuditReportError("invalid evidence path")
    if any(ord(character) < 32 for character in value):
        raise ContextAuditReportError("invalid evidence path")
    return path.as_posix()


def _safe_file(root: Path, relative: str) -> Path:
    path = root.joinpath(*PurePosixPath(relative).parts)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root.parent):
        raise ContextAuditReportError("symlinked report input")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ContextAuditReportError("report path escapes configured root") from exc
    if not resolved.is_file():
        raise ContextAuditReportError("report input is not a file")
    return resolved


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ContextAuditReportError("report JSON is too large")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContextAuditReportError("invalid report JSON") from exc
    if not isinstance(value, dict):
        raise ContextAuditReportError("report must be an object")
    return value


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ContextAuditReportError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ContextAuditReportError(f"{field} must be an ISO date") from exc


def _integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContextAuditReportError(f"{field} is invalid")
    return value


def _normalize_evidence(
    item: Any,
    *,
    allowed_repositories: set[str],
    allowed_revisions: set[str],
) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ContextAuditReportError("evidence must be an object")
    path = _safe_relative_path(item.get("path"))
    line = _integer(item.get("line"), "evidence line", 1, 10_000_000)
    url = item.get("url")
    if not isinstance(url, str):
        raise ContextAuditReportError("evidence URL is invalid")
    match = EVIDENCE_URL_RE.fullmatch(url)
    if not match:
        raise ContextAuditReportError("evidence URL is not canonical")
    if match["repository"] not in allowed_repositories:
        raise ContextAuditReportError("evidence repository is not audited")
    if match["revision"] not in allowed_revisions:
        raise ContextAuditReportError("evidence revision is not audited")
    if _safe_relative_path(match["path"]) != path or int(match["line"]) != line:
        raise ContextAuditReportError("evidence URL does not match its reference")
    return {
        "label": f"{path}:L{line}",
        "path": path,
        "line": line,
        "url": url,
    }


def _normalize_evidence_list(
    items: Any,
    *,
    allowed_repositories: set[str],
    allowed_revisions: set[str],
) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ContextAuditReportError("evidence list is required")
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        reference = _normalize_evidence(
            item,
            allowed_repositories=allowed_repositories,
            allowed_revisions=allowed_revisions,
        )
        key = (reference["path"], reference["line"], reference["url"])
        if key not in seen:
            normalized.append(reference)
            seen.add(key)
    return normalized


def _load_inventory(root: Path) -> dict[str, Any]:
    inventory = _read_json(_safe_file(root, "repositories.json"))
    if inventory.get("version") != 1:
        raise ContextAuditReportError("unsupported inventory version")
    observed_at = _iso_date(inventory.get("observed_at"), "inventory observed_at")
    source = inventory.get("evidence_source")
    if not isinstance(source, dict):
        raise ContextAuditReportError("inventory evidence source is required")
    repository = source.get("repository")
    revision = source.get("ref")
    source_path = _safe_relative_path(source.get("path"))
    if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
        raise ContextAuditReportError("inventory source repository is invalid")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ContextAuditReportError("inventory source revision is invalid")
    source_url = f"https://github.com/{repository}/blob/{revision}/{source_path}"
    return {
        "observed_at": observed_at,
        "repository": repository,
        "revision": revision,
        "url": source_url,
    }


def _canonical_candidates(root: Path) -> list[tuple[date, int, str, Path]]:
    candidates: list[tuple[date, int, str, Path]] = []
    for kind, directory_name in (("baseline", "baselines"), ("delta", "deltas")):
        directory = root / directory_name
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in directory.iterdir():
            match = REPORT_PATTERNS[kind].fullmatch(path.name)
            if not match:
                continue
            observed_at = _iso_date(match["date"], "report filename")
            candidates.append((observed_at, 1 if kind == "delta" else 0, path.name, path))
    return sorted(candidates)


def _validate_report_reference(root: Path, value: Any, kind: str) -> None:
    prefix = f"repository-context/{kind}s/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ContextAuditReportError(f"{kind} reference is invalid")
    filename = value.removeprefix(prefix)
    if "/" in filename or not REPORT_PATTERNS[kind].fullmatch(filename):
        raise ContextAuditReportError(f"{kind} reference is not canonical")
    _safe_file(root, f"{kind}s/{filename}")


def _normalize_report(
    root: Path,
    report: dict[str, Any],
    *,
    inventory: dict[str, Any],
    today: date,
    stale_after_days: int,
) -> dict[str, Any]:
    if report.get("schema_version") != 1:
        raise ContextAuditReportError("unsupported report version")
    kind = report.get("kind")
    if kind not in REPORT_PATTERNS:
        raise ContextAuditReportError("unsupported report kind")
    observed_at = _iso_date(report.get("observed_at"), "report observed_at")
    if report.get("inventory") != "repository-context/repositories.json":
        raise ContextAuditReportError("inventory reference is not canonical")
    if kind == "delta":
        _validate_report_reference(root, report.get("baseline"), "baseline")
    if report.get("actions") != []:
        raise ContextAuditReportError("report actions must remain empty")

    weights = report.get("weights")
    if (
        not isinstance(weights, dict)
        or set(weights) != set(SUBSCORES)
        or any(type(value) is not int or value < 0 for value in weights.values())
        or sum(weights.values()) != 100
    ):
        raise ContextAuditReportError("report weights are invalid")

    raw_repositories = report.get("repositories")
    repository_count = report.get("repository_count")
    if (
        not isinstance(raw_repositories, list)
        or not raw_repositories
        or type(repository_count) is not int
        or repository_count != len(raw_repositories)
    ):
        raise ContextAuditReportError("repository count is invalid")

    audited_repositories: set[str] = set()
    audited_revisions: set[str] = {inventory["revision"]}
    for item in raw_repositories:
        if not isinstance(item, dict):
            raise ContextAuditReportError("repository record is invalid")
        name = item.get("name")
        revision = item.get("audit_ref")
        if not isinstance(name, str) or not REPOSITORY_RE.fullmatch(name):
            raise ContextAuditReportError("repository name is invalid")
        if name in audited_repositories:
            raise ContextAuditReportError("repository names must be unique")
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            raise ContextAuditReportError("repository revision is invalid")
        audited_repositories.add(name)
        audited_revisions.add(revision)

    allowed_evidence_repositories = audited_repositories | {inventory["repository"]}
    repositories: list[dict[str, Any]] = []
    for item in raw_repositories:
        score = _integer(item.get("score"), "repository score", 0, 100)
        raw_subscores = item.get("subscores")
        if not isinstance(raw_subscores, dict) or set(raw_subscores) != set(SUBSCORES):
            raise ContextAuditReportError("repository subscores are invalid")
        subscores = {
            name: _integer(raw_subscores[name], f"{name} subscore", 0, 100)
            for name in SUBSCORES
        }
        baseline_score = item.get("baseline_score")
        change = item.get("change")
        if baseline_score is not None:
            baseline_score = _integer(baseline_score, "baseline score", 0, 100)
        if change is not None and (type(change) is not int or not -100 <= change <= 100):
            raise ContextAuditReportError("score change is invalid")
        if kind == "delta" and (
            baseline_score is None or change != score - baseline_score
        ):
            raise ContextAuditReportError("delta score change is inconsistent")
        repositories.append(
            {
                "name": item["name"],
                "source_revision": item["audit_ref"],
                "score": score,
                "baseline_score": baseline_score,
                "change": change,
                "subscores": subscores,
                "evidence": _normalize_evidence_list(
                    item.get("evidence"),
                    allowed_repositories=allowed_evidence_repositories,
                    allowed_revisions=audited_revisions,
                ),
            }
        )

    raw_findings = report.get("findings")
    if not isinstance(raw_findings, list):
        raise ContextAuditReportError("findings must be a list")
    findings: list[dict[str, Any]] = []
    for item in raw_findings:
        if not isinstance(item, dict) or item.get("reason") not in {
            "score_below_60",
            "score_drop",
        }:
            raise ContextAuditReportError("finding is invalid")
        repository = item.get("repository")
        if repository not in audited_repositories:
            raise ContextAuditReportError("finding repository is invalid")
        finding_id = item.get("id")
        if not isinstance(finding_id, str) or not finding_id or len(finding_id) > 200:
            raise ContextAuditReportError("finding id is invalid")
        findings.append(
            {
                "repository": repository,
                "reason": item["reason"],
                "score": _integer(item.get("score"), "finding score", 0, 100),
                "baseline_score": (
                    None
                    if item.get("baseline_score") is None
                    else _integer(item["baseline_score"], "finding baseline score", 0, 100)
                ),
                "evidence": _normalize_evidence_list(
                    item.get("evidence"),
                    allowed_repositories=allowed_evidence_repositories,
                    allowed_revisions=audited_revisions,
                ),
            }
        )

    repositories.sort(key=lambda item: (item["score"], item["name"]))
    passing = sum(item["score"] >= 60 for item in repositories)
    changes = [item["change"] for item in repositories if item["change"] is not None]
    age_days = (today - observed_at).days
    return {
        "available": True,
        "report_kind": kind,
        "observed_at": observed_at.isoformat(),
        "age_days": age_days,
        "stale": age_days > stale_after_days,
        "stale_after_days": stale_after_days,
        "inventory_observed_at": inventory["observed_at"].isoformat(),
        "inventory_revision": inventory["revision"],
        "source": {
            "repository": inventory["repository"],
            "revision": inventory["revision"],
            "url": inventory["url"],
        },
        "weights": {name: weights[name] for name in SUBSCORES},
        "summary": {
            "repository_count": len(repositories),
            "passing_repositories": passing,
            "coverage_percent": round(passing / len(repositories) * 100),
            "average_score": round(
                sum(item["score"] for item in repositories) / len(repositories)
            ),
            "threshold_findings": len(findings),
        },
        "delta": {
            "improved": sum(change > 0 for change in changes),
            "declined": sum(change < 0 for change in changes),
            "unchanged": sum(change == 0 for change in changes),
            "largest_drop": min(changes) if changes else None,
        },
        "repositories": repositories,
        "findings": findings,
    }


def load_context_audit(
    report_root: Path | str | None = None,
    *,
    today: date | None = None,
    stale_after_days: int | None = None,
) -> dict[str, Any]:
    """Load the newest canonical report and return only dashboard-safe fields."""
    root_path = Path(report_root) if report_root is not None else DEFAULT_REPORT_ROOT
    if (
        not root_path.is_absolute()
        or not root_path.exists()
        or not root_path.is_dir()
        or root_path.is_symlink()
    ):
        return _unavailable("report root unavailable")
    try:
        root = root_path.resolve(strict=True)
        inventory = _load_inventory(root)
        candidates = _canonical_candidates(root)
        if not candidates:
            return _unavailable("no valid canonical reports")
        _, _, _, candidate = candidates[-1]
        report_path = _safe_file(root, candidate.relative_to(root).as_posix())
        report = _read_json(report_path)
        payload = _normalize_report(
            root,
            report,
            inventory=inventory,
            today=today or date.today(),
            stale_after_days=(
                DEFAULT_STALE_AFTER_DAYS
                if stale_after_days is None
                else stale_after_days
            ),
        )
        filename_match = REPORT_PATTERNS[payload["report_kind"]].fullmatch(candidate.name)
        if not filename_match or filename_match["date"] != payload["observed_at"]:
            raise ContextAuditReportError("report filename does not match observed date")
        return payload
    except (ContextAuditReportError, OSError, ValueError):
        return _unavailable("no valid canonical reports")
