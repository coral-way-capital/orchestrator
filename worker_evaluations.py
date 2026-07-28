#!/usr/bin/env python3
"""Worker evaluation registry and recommendation-only feedback loop.

Evaluations are derived from terminal PR engineering outcomes plus explicit
review, commit, reopening, follow-up, and human-override evidence. Missing
signals remain ``not_available``. This module has deliberately no dispatcher
dependency and no function capable of changing prompt/model routing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


EVALUATIONS_DB = Path(
    os.environ.get(
        "CWC_WORKER_EVALUATIONS_DB",
        str(Path.home() / ".hermes" / "issue-queue" / "worker-evaluations.db"),
    )
)
TERMINAL_STATES = {"merged", "closed_unmerged"}
SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
SEVERITY_PATTERN = re.compile(
    r"\[(?:severity|sev)\s*:\s*(critical|high|medium|low)\]", re.IGNORECASE
)
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_evaluations (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    item_id TEXT,
    prompt_id TEXT,
    model_provider TEXT,
    model TEXT,
    task_class TEXT,
    terminal_state TEXT NOT NULL,
    terminal_at TEXT NOT NULL,
    primary_failure TEXT,
    evaluated_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    evaluation_json TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number)
);
CREATE INDEX IF NOT EXISTS idx_worker_evaluation_digest
    ON worker_evaluations(evaluated_at, primary_failure, prompt_id, model, task_class);
CREATE INDEX IF NOT EXISTS idx_worker_evaluation_terminal_digest
    ON worker_evaluations(terminal_at, primary_failure, prompt_id, model, task_class);
"""


class EvaluationError(ValueError):
    """Evaluation input cannot be represented without inventing evidence."""


def _timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise EvaluationError(f"{field} is required")
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvaluationError(f"{field} must include a timezone")
    return text


def _parsed_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_db(db_path: Path | str = EVALUATIONS_DB):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _not_available(reason: str) -> dict[str, Any]:
    return {
        "value": "not_available",
        "availability": "not_available",
        "reason": reason,
        "evidence": [],
    }


def _evidence(
    source: dict[str, Any],
    *,
    kind: str,
    require_url: bool = True,
) -> dict[str, Any] | None:
    url = source.get("html_url") or source.get("url")
    if require_url and not url:
        return None
    result = {
        "kind": kind,
        "availability": "available" if url else "not_available",
        "url": url,
    }
    for key in ("id", "sha", "commit_id", "submitted_at", "committed_at"):
        if source.get(key) is not None:
            result[key] = source[key]
    return result


def _review_severity(reviews: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [
        review
        for review in reviews
        if str(review.get("state", "")).upper() == "CHANGES_REQUESTED"
    ]
    if not changes:
        return _not_available("no changes-requested review evidence")

    classified: list[tuple[str, dict[str, Any]]] = []
    for review in changes:
        match = SEVERITY_PATTERN.search(str(review.get("body") or ""))
        if match:
            classified.append((match.group(1).lower(), review))
    if not classified:
        result = _not_available(
            "changes-requested reviews do not contain an explicit severity tag"
        )
        result["unclassified_review_count"] = len(changes)
        return result

    highest = max(
        (severity for severity, _ in classified), key=SEVERITY_ORDER.get
    )
    evidence = [
        entry
        for severity, review in classified
        if severity == highest
        for entry in [_evidence(review, kind="github_review")]
        if entry is not None
    ]
    return {
        "value": highest,
        "availability": "available",
        "classification_method": "explicit_review_severity_tag",
        "evidence": evidence,
        "evidence_gap": (
            None if len(evidence) == sum(s == highest for s, _ in classified)
            else "one or more classified reviews lack an exact GitHub URL"
        ),
    }


def _fix_up_ratio(
    reviews: list[dict[str, Any]], commits: list[dict[str, Any]]
) -> dict[str, Any]:
    change_times = [
        _timestamp(review.get("submitted_at"), "review.submitted_at")
        for review in reviews
        if str(review.get("state", "")).upper() == "CHANGES_REQUESTED"
    ]
    if not change_times:
        return _not_available("no changes-requested review establishes a fix-up boundary")
    if not commits:
        return _not_available("commit history is unavailable")

    normalized_commits = []
    for commit in commits:
        committed_at = _timestamp(commit.get("committed_at"), "commit.committed_at")
        normalized_commits.append((committed_at, commit))
    boundary = min(change_times, key=_parsed_timestamp)
    fixups = [
        commit
        for committed_at, commit in normalized_commits
        if _parsed_timestamp(committed_at) > _parsed_timestamp(boundary)
    ]
    evidence = [
        item
        for commit in fixups
        for item in [_evidence(commit, kind="github_commit")]
        if item is not None
    ]
    return {
        "value": round(len(fixups) / len(normalized_commits), 4),
        "availability": "available",
        "numerator": len(fixups),
        "denominator": len(normalized_commits),
        "definition": "commits after first changes-requested review / all PR commits",
        "boundary_at": boundary,
        "evidence": evidence,
        "evidence_gap": (
            None if len(evidence) == len(fixups)
            else "one or more fix-up commits lack an exact GitHub URL"
        ),
    }


def _reopen_follow_up(pull: dict[str, Any]) -> dict[str, Any]:
    has_reopening_signal = "reopenings" in pull
    has_follow_up_signal = "follow_ups" in pull
    if not has_reopening_signal and not has_follow_up_signal:
        return _not_available("reopening and follow-up signals are unavailable")
    reopenings = list(pull.get("reopenings") or [])
    follow_ups = list(pull.get("follow_ups") or [])
    if reopenings and follow_ups:
        value = "reopened_and_follow_up"
    elif reopenings:
        value = "reopened"
    elif follow_ups:
        value = "follow_up"
    else:
        value = "none_observed"
    sources = [("github_reopening", row) for row in reopenings]
    sources.extend(("github_follow_up", row) for row in follow_ups)
    evidence = [
        entry
        for kind, source in sources
        for entry in [_evidence(source, kind=kind)]
        if entry is not None
    ]
    return {
        "value": value,
        "availability": (
            "available"
            if has_reopening_signal and has_follow_up_signal
            else "partial"
        ),
        "reopening_count": len(reopenings),
        "follow_up_count": len(follow_ups),
        "evidence": evidence,
        "evidence_gap": (
            None if len(evidence) == len(sources)
            else "one or more reopening/follow-up signals lack an exact GitHub URL"
        ),
    }


def _human_override(pull: dict[str, Any]) -> dict[str, Any]:
    if "human_override" not in pull:
        return _not_available("human override signal is unavailable")
    override = pull.get("human_override")
    if not override:
        return {
            "value": "none_observed",
            "availability": "available",
            "actor": None,
            "reason": None,
            "evidence": [],
        }
    evidence = _evidence(override, kind="github_comment")
    return {
        "value": str(override.get("decision") or "unknown"),
        "availability": "available",
        "actor": override.get("actor"),
        "reason": override.get("reason"),
        "evidence": [evidence] if evidence else [],
        "evidence_gap": None if evidence else "override lacks an exact GitHub comment URL",
    }


def _primary_failure(evaluation: dict[str, Any]) -> str | None:
    if evaluation["human_override"]["value"] == "wrong_direction":
        return "wrong_direction"
    reopen = evaluation["reopen_or_follow_up"]["value"]
    if reopen in {"reopened", "reopened_and_follow_up"}:
        return "reopened"
    severity = evaluation["review_severity"]["value"]
    if severity in {"critical", "high"}:
        return "severe_review"
    ratio = evaluation["fix_up_ratio"]["value"]
    if isinstance(ratio, (int, float)) and ratio >= 0.5:
        return "high_fix_up_ratio"
    if evaluation["merge_state"]["value"] == "closed_unmerged":
        return "closed_unmerged"
    if reopen == "follow_up":
        return "follow_up"
    return None


def evaluate_pull(
    pull: dict[str, Any], *, evaluated_at: str | None = None
) -> dict[str, Any]:
    """Build one evaluation without inferring unavailable evidence."""
    state = str(pull.get("state") or "")
    if state not in TERMINAL_STATES:
        raise EvaluationError("only terminal tracked PRs can be evaluated")
    repo = str(pull.get("repo") or "")
    pr_number = pull.get("pr_number")
    if not repo or isinstance(pr_number, bool) or not isinstance(pr_number, int):
        raise EvaluationError("repo and positive integer pr_number are required")
    opened_at = _timestamp(pull.get("opened_at"), "opened_at")
    merged_at = _timestamp(pull.get("merged_at"), "merged_at", required=False)
    closed_at = _timestamp(pull.get("closed_at"), "closed_at", required=False)
    terminal_at = merged_at if state == "merged" else closed_at
    if terminal_at is None:
        raise EvaluationError(f"{state} PR requires its exact terminal timestamp")
    evaluated_at = _timestamp(evaluated_at or _now_iso(), "evaluated_at")
    pr_url = pull.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
    time_seconds = (
        int((_parsed_timestamp(merged_at) - _parsed_timestamp(opened_at)).total_seconds())
        if merged_at
        else None
    )
    reviews = list(pull.get("reviews") or [])
    commits = list(pull.get("commits") or [])

    evaluation = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "pr_number": pr_number,
        "item_id": pull.get("item_id"),
        "prompt_id": pull.get("prompt_id") or "not_available",
        "model_provider": pull.get("model_provider") or "not_available",
        "model": pull.get("model") or "not_available",
        "task_class": pull.get("task_class") or "not_available",
        "evaluated_at": evaluated_at,
        "terminal_at": terminal_at,
        "merge_state": {
            "value": state,
            "availability": "available",
            "evidence": [{"kind": "github_pull_request", "url": pr_url}],
        },
        "review_severity": _review_severity(reviews),
        "fix_up_ratio": _fix_up_ratio(reviews, commits),
        "time_to_merge": {
            "seconds": time_seconds,
            "availability": "available" if time_seconds is not None else "not_available",
            "reason": None if time_seconds is not None else "PR was not merged",
            "evidence": (
                [{"kind": "github_pull_request", "url": pr_url}]
                if time_seconds is not None
                else []
            ),
        },
        "reopen_or_follow_up": _reopen_follow_up(pull),
        "human_override": _human_override(pull),
    }
    evaluation["primary_failure"] = _primary_failure(evaluation)
    return evaluation


def _upsert(db: sqlite3.Connection, evaluation: dict[str, Any]) -> None:
    db.execute(
        """
        INSERT INTO worker_evaluations
          (repo, pr_number, item_id, prompt_id, model_provider, model, task_class,
           terminal_state, terminal_at, primary_failure, evaluated_at, schema_version,
           evaluation_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, pr_number) DO UPDATE SET
          item_id=excluded.item_id,
          prompt_id=excluded.prompt_id,
          model_provider=excluded.model_provider,
          model=excluded.model,
          task_class=excluded.task_class,
          terminal_state=excluded.terminal_state,
          terminal_at=excluded.terminal_at,
          primary_failure=excluded.primary_failure,
          evaluated_at=excluded.evaluated_at,
          schema_version=excluded.schema_version,
          evaluation_json=excluded.evaluation_json
        """,
        (
            evaluation["repo"],
            evaluation["pr_number"],
            evaluation.get("item_id"),
            evaluation["prompt_id"],
            evaluation["model_provider"],
            evaluation["model"],
            evaluation["task_class"],
            evaluation["merge_state"]["value"],
            evaluation["terminal_at"],
            evaluation["primary_failure"],
            evaluation["evaluated_at"],
            evaluation["schema_version"],
            json.dumps(evaluation, sort_keys=True, separators=(",", ":")),
        ),
    )


def list_evaluations(
    *, db_path: Path | str = EVALUATIONS_DB
) -> list[dict[str, Any]]:
    with get_db(db_path) as db:
        rows = db.execute(
            "SELECT evaluation_json FROM worker_evaluations ORDER BY repo, pr_number"
        ).fetchall()
    return [json.loads(row["evaluation_json"]) for row in rows]


def refresh_registry(
    pulls: Iterable[dict[str, Any]],
    *,
    db_path: Path | str = EVALUATIONS_DB,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """Evaluate every terminal input and return explicit coverage."""
    terminal = sorted(
        (pull for pull in pulls if pull.get("state") in TERMINAL_STATES),
        key=lambda pull: (str(pull.get("repo") or ""), int(pull.get("pr_number") or 0)),
    )
    evaluations = [
        evaluate_pull(pull, evaluated_at=evaluated_at) for pull in terminal
    ]
    with get_db(db_path) as db:
        for evaluation in evaluations:
            _upsert(db, evaluation)
    evaluated = len(evaluations)
    return {
        "coverage": {
            "terminal_tracked": len(terminal),
            "evaluated": evaluated,
            "percent": round(evaluated / len(terminal) * 100, 1) if terminal else 100.0,
        },
        "evaluations": evaluations,
    }


def _queue_context(item_id: str | None, queue_file: Path | str) -> dict[str, Any]:
    if not item_id:
        return {}
    try:
        queue = json.loads(Path(queue_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    matches = [
        item
        for bucket in ("pending", "in_progress", "completed", "failed")
        for item in queue.get(bucket, [])
        if item.get("id") == item_id
    ]
    if len(matches) != 1:
        return {}
    item = matches[0]
    return {
        "prompt_id": item.get("agent_prompt"),
        "model_provider": item.get("model_provider"),
        "model": item.get("model"),
        "task_class": item.get("task_class"),
    }


def refresh_from_pr_outcomes(
    *,
    pr_db_path: Path | str,
    db_path: Path | str = EVALUATIONS_DB,
    queue_file: Path | str | None = None,
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """Read terminal PRs from #17's ledger without calling or mutating GitHub."""
    import pr_outcomes

    report = pr_outcomes.build_report(db_path=pr_db_path)
    pulls = []
    for outcome in report["pull_requests"]:
        if outcome["state"] not in TERMINAL_STATES:
            continue
        events = pr_outcomes.list_pull_events(
            outcome["repo"], outcome["pr_number"], db_path=pr_db_path
        )
        reviews = []
        reopenings = []
        for event in events:
            payload = event.get("payload") or {}
            if event["event_type"] in {"approved", "changes_requested"}:
                reviews.append(
                    {
                        "id": payload.get("review_id"),
                        "state": payload.get("state"),
                        "submitted_at": event["event_at"],
                        "body": payload.get("body"),
                        "html_url": payload.get("html_url"),
                        "commit_id": payload.get("commit_id"),
                    }
                )
            elif (
                event["event_type"] == "opened"
                and ":reopened:" in event["event_key"]
            ):
                reopenings.append(
                    {
                        "html_url": payload.get("html_url"),
                        "occurred_at": event["event_at"],
                    }
                )
        pull = {
            **outcome,
            "reviews": reviews,
            "commits": [],
            "reopenings": reopenings,
        }
        if queue_file is not None:
            pull.update(_queue_context(outcome.get("item_id"), queue_file))
        pulls.append(pull)
    return refresh_registry(pulls, db_path=db_path, evaluated_at=evaluated_at)


PROPOSED_CHANGES = {
    "wrong_direction": (
        "prompt_review",
        "Add a pre-implementation wrong-direction check with explicit "
        "human-verifiable scope evidence to the affected prompt.",
    ),
    "reopened": (
        "prompt_review",
        "Require reopening-risk checks and linked acceptance evidence before closure.",
    ),
    "severe_review": (
        "prompt_review",
        "Add a severity-focused self-review pass before requesting review.",
    ),
    "high_fix_up_ratio": (
        "prompt_review",
        "Add a review-readiness checklist before the first review request.",
    ),
    "closed_unmerged": (
        "task_class_review",
        "Review task classification and escalation guidance for unmerged closures.",
    ),
    "follow_up": (
        "prompt_review",
        "Add follow-up issue detection to the completion checklist.",
    ),
}


def build_weekly_digest(
    *,
    week_start: str,
    db_path: Path | str = EVALUATIONS_DB,
) -> dict[str, Any]:
    """Aggregate one stable weekly failure recommendation digest."""
    start_text = _timestamp(week_start, "week_start")
    start = _parsed_timestamp(start_text)
    end = start + timedelta(days=7)
    with get_db(db_path) as db:
        rows = db.execute(
            """
            SELECT primary_failure, prompt_id, model_provider, model, task_class,
                   COUNT(*) AS failure_count
            FROM worker_evaluations
            WHERE terminal_at >= ? AND terminal_at < ?
              AND primary_failure IS NOT NULL
            GROUP BY primary_failure, prompt_id, model_provider, model, task_class
            HAVING COUNT(*) >= 2
            ORDER BY failure_count DESC, primary_failure, prompt_id,
                     model_provider, model, task_class
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    top = dict(rows[0]) if rows else None
    if top:
        top_failure = {
            "failure": top["primary_failure"],
            "count": top["failure_count"],
            "prompt_id": top["prompt_id"],
            "model_provider": top["model_provider"],
            "model": top["model"],
            "task_class": top["task_class"],
        }
        change_type, description = PROPOSED_CHANGES[top["primary_failure"]]
        proposed_change = {
            "type": change_type,
            "description": description,
            "status": "proposed_not_applied",
        }
    else:
        top_failure = None
        proposed_change = {
            "type": "none",
            "description": "No repeated failure was observed in this window.",
            "status": "no_change_proposed",
        }
    return {
        "week_start": start_text,
        "week_end": end.isoformat(),
        "top_repeated_failure": top_failure,
        "proposed_system_change": proposed_change,
        "routing": {
            "mode": "recommendations_only",
            "automatic_mutation": False,
            "approval_required": "ivan",
            "minimum_observation_days": 30,
        },
    }


def routing_gate(
    *,
    observed_at: str,
    now: str | None = None,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Report the approval boundary; never enable or mutate routing."""
    observed_text = _timestamp(observed_at, "observed_at")
    now_text = _timestamp(now or _now_iso(), "now")
    eligible_at = _parsed_timestamp(observed_text) + timedelta(days=30)
    period_complete = _parsed_timestamp(now_text) >= eligible_at
    if approval is None:
        approval_state = "not_available"
    elif str(approval.get("actor") or "").lower() != "ivan":
        approval_state = "invalid_approver"
    elif not approval.get("approved_at"):
        approval_state = "not_available"
    else:
        _timestamp(approval["approved_at"], "approval.approved_at")
        approval_state = "approved_boundary_only"
    return {
        "automatic_routing_enabled": False,
        "observation_period_complete": period_complete,
        "eligible_at": eligible_at.isoformat(),
        "approval_state": approval_state,
        "required_approver": "ivan",
        "reason": "automatic routing remains disabled in code",
    }


def main(argv: list[str] | None = None) -> int:
    """Refresh the registry and print a weekly recommendation digest."""
    import pr_outcomes

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr-outcomes-db", default=str(pr_outcomes.OUTCOMES_DB))
    parser.add_argument("--evaluations-db", default=str(EVALUATIONS_DB))
    parser.add_argument("--queue-file", default=str(pr_outcomes.QUEUE_FILE))
    parser.add_argument("--week-start", required=True)
    parser.add_argument("--evaluated-at")
    args = parser.parse_args(argv)
    report = refresh_from_pr_outcomes(
        pr_db_path=args.pr_outcomes_db,
        db_path=args.evaluations_db,
        queue_file=args.queue_file,
        evaluated_at=args.evaluated_at,
    )
    digest = build_weekly_digest(
        week_start=args.week_start, db_path=args.evaluations_db
    )
    print(json.dumps({"registry": report, "weekly_digest": digest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
