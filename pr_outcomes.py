#!/usr/bin/env python3
"""Idempotent pull-request engineering outcomes and 30-day backfill.

PR state is engineering evidence only.  This module deliberately has no path
that can set business acceptance; that field is always ``not_available`` until
an independent, human-owned business signal exists.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable


OUTCOMES_DB = Path(
    os.environ.get(
        "CWC_PR_OUTCOMES_DB",
        str(Path.home() / ".hermes" / "issue-queue" / "pr-outcomes.db"),
    )
)
QUEUE_FILE = Path(
    os.environ.get(
        "CWC_QUEUE_FILE",
        str(Path.home() / ".hermes" / "issue-queue" / "queue.json"),
    )
)
CURRENT_STATES = {
    "opened",
    "approved",
    "changes_requested",
    "merged",
    "closed_unmerged",
}
TERMINAL_STATES = {"merged", "closed_unmerged"}
REVIEW_STATES = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes_requested"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS pull_requests (
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    github_id INTEGER,
    html_url TEXT,
    state TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    updated_at TEXT,
    merged_at TEXT,
    closed_at TEXT,
    item_id TEXT,
    dispatch_id TEXT,
    project_id TEXT,
    worker_result_id INTEGER,
    linkage_state TEXT NOT NULL DEFAULT 'not_available',
    business_acceptance_state TEXT NOT NULL DEFAULT 'not_available'
        CHECK (business_acceptance_state = 'not_available'),
    source TEXT NOT NULL,
    PRIMARY KEY (repo, pr_number)
);

CREATE TABLE IF NOT EXISTS pr_outcome_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    event_key TEXT NOT NULL UNIQUE,
    github_actor TEXT,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pr_events_pull
    ON pr_outcome_events(repo, pr_number, event_at, id);

CREATE TABLE IF NOT EXISTS pr_backfill_runs (
    repo TEXT PRIMARY KEY,
    window_started_at TEXT NOT NULL,
    last_pr_number INTEGER,
    last_completed_at TEXT,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class PROutcomeError(ValueError):
    """A GitHub payload cannot be represented honestly."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exact_timestamp(value: Any, field: str, *, required: bool = True) -> str | None:
    if value in (None, ""):
        if required:
            raise PROutcomeError(f"{field} is required")
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PROutcomeError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PROutcomeError(f"{field} must include a timezone")
    return text


@contextmanager
def get_db(db_path: Path | str = OUTCOMES_DB):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _empty_context(state: str = "not_available") -> dict[str, Any]:
    return {
        "item_id": None,
        "dispatch_id": None,
        "project_id": None,
        "worker_result_id": None,
        "linkage_state": state,
    }


def resolve_context(repo: str, pr_number: int) -> dict[str, Any]:
    """Resolve structured-result, queue, dispatch, and portfolio links.

    Ambiguous numeric-only matches are intentionally rejected.  A worker result
    links cross-repository tracking issues by an exact PR URL in its evidence.
    """
    result = _empty_context()
    pr_url = f"https://github.com/{repo}/pull/{pr_number}"
    worker_row = None
    try:
        import worker_results

        candidates = []
        for row in worker_results.list_results():
            if row.get("pr_number") != pr_number:
                continue
            try:
                evidence = json.loads(row.get("evidence_json") or "{}")
            except json.JSONDecodeError:
                evidence = {}
            evidence_urls = {
                value.rstrip("/")
                for key, value in evidence.items()
                if isinstance(value, str) and ("url" in key or value.startswith("https://"))
            }
            if pr_url in evidence_urls:
                candidates.append(row)
        if len(candidates) == 1:
            worker_row = candidates[0]
    except Exception:
        worker_row = None

    queue_item = None
    try:
        queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        items = [
            item
            for bucket in ("pending", "in_progress", "completed", "failed")
            for item in queue.get(bucket, [])
        ]
        if worker_row:
            queue_item = next(
                (item for item in items if item.get("id") == worker_row.get("item_id")),
                None,
            )
        if queue_item is None:
            exact = [
                item for item in items
                if item.get("repo") == repo and item.get("pr_number") == pr_number
            ]
            if len(exact) == 1:
                queue_item = exact[0]
    except (OSError, ValueError):
        queue_item = None

    project_id = None
    try:
        from portfolio import load_portfolio

        projects = [
            project for project in load_portfolio().get("projects", [])
            if project.get("repo") == repo
        ]
        if len(projects) == 1:
            project_id = projects[0].get("id")
    except Exception:
        project_id = None

    item_id = (worker_row or {}).get("item_id") or (queue_item or {}).get("id")
    dispatch_id = (
        (worker_row or {}).get("dispatch_id")
        or (queue_item or {}).get("dispatch_id")
    )
    if item_id or dispatch_id or project_id or worker_row:
        result.update(
            {
                "item_id": item_id,
                "dispatch_id": dispatch_id,
                "project_id": project_id,
                "worker_result_id": (worker_row or {}).get("id"),
                "linkage_state": "linked" if item_id else "unknown",
            }
        )
    return result


def _insert_event(
    db: sqlite3.Connection,
    *,
    repo: str,
    pr_number: int,
    event_type: str,
    event_at: str,
    event_key: str,
    actor: str | None,
    source: str,
    payload: dict[str, Any],
) -> int:
    if event_type not in CURRENT_STATES:
        raise PROutcomeError(f"unsupported PR event type: {event_type}")
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO pr_outcome_events
          (repo, pr_number, event_type, event_at, event_key, github_actor,
           source, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            repo,
            pr_number,
            event_type,
            event_at,
            event_key,
            actor,
            source,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
    )
    return cursor.rowcount


def _derived_state(db: sqlite3.Connection, repo: str, pr_number: int) -> str:
    rows = db.execute(
        """
        SELECT event_type FROM pr_outcome_events
        WHERE repo = ? AND pr_number = ?
        ORDER BY event_at DESC, id DESC
        """,
        (repo, pr_number),
    ).fetchall()
    types = [row["event_type"] for row in rows]
    if "merged" in types:
        return "merged"
    return types[0] if types else "opened"


def _upsert_pull(
    db: sqlite3.Connection,
    repo: str,
    pull: dict[str, Any],
    *,
    source: str,
    context: dict[str, Any],
) -> None:
    number = pull.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PROutcomeError("pull_request.number must be a positive integer")
    opened_at = _exact_timestamp(pull.get("created_at"), "pull_request.created_at")
    state = _derived_state(db, repo, number)
    db.execute(
        """
        INSERT INTO pull_requests
          (repo, pr_number, github_id, html_url, state, opened_at, updated_at,
           merged_at, closed_at, item_id, dispatch_id, project_id,
           worker_result_id, linkage_state, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(repo, pr_number) DO UPDATE SET
          github_id=COALESCE(excluded.github_id, pull_requests.github_id),
          html_url=COALESCE(excluded.html_url, pull_requests.html_url),
          state=excluded.state,
          opened_at=excluded.opened_at,
          updated_at=COALESCE(excluded.updated_at, pull_requests.updated_at),
          merged_at=COALESCE(excluded.merged_at, pull_requests.merged_at),
          closed_at=COALESCE(excluded.closed_at, pull_requests.closed_at),
          item_id=COALESCE(excluded.item_id, pull_requests.item_id),
          dispatch_id=COALESCE(excluded.dispatch_id, pull_requests.dispatch_id),
          project_id=COALESCE(excluded.project_id, pull_requests.project_id),
          worker_result_id=COALESCE(
              excluded.worker_result_id, pull_requests.worker_result_id
          ),
          linkage_state=CASE
              WHEN excluded.linkage_state = 'linked' THEN 'linked'
              ELSE pull_requests.linkage_state
          END,
          source=excluded.source
        """,
        (
            repo,
            number,
            pull.get("id"),
            pull.get("html_url"),
            state,
            opened_at,
            _exact_timestamp(pull.get("updated_at"), "pull_request.updated_at", required=False),
            _exact_timestamp(pull.get("merged_at"), "pull_request.merged_at", required=False),
            _exact_timestamp(pull.get("closed_at"), "pull_request.closed_at", required=False),
            context.get("item_id"),
            context.get("dispatch_id"),
            context.get("project_id"),
            context.get("worker_result_id"),
            context.get("linkage_state") or "not_available",
            source,
        ),
    )


def ingest_pull_snapshot(
    repo: str,
    pull: dict[str, Any],
    reviews: list[dict[str, Any]] | None = None,
    *,
    db_path: Path | str = OUTCOMES_DB,
    context_resolver: Callable[[str, int], dict[str, Any]] = resolve_context,
    source: str = "github_backfill",
    event_logger: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Ingest one exact GitHub pull snapshot and its reviews."""
    if not repo or "/" not in repo:
        raise PROutcomeError("repository.full_name is required")
    number = pull.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise PROutcomeError("pull_request.number must be a positive integer")
    github_id = pull.get("id") or f"{repo}#{number}"
    opened_at = _exact_timestamp(pull.get("created_at"), "pull_request.created_at")
    inserted: list[tuple[str, str]] = []
    context = context_resolver(repo, number)
    with get_db(db_path) as db:
        if _insert_event(
            db,
            repo=repo,
            pr_number=number,
            event_type="opened",
            event_at=opened_at,
            event_key=f"pull:{github_id}:opened",
            actor=None,
            source=source,
            payload={"html_url": pull.get("html_url")},
        ):
            inserted.append(("opened", opened_at))
        reopened_at = _exact_timestamp(
            pull.get("_reopened_at"), "pull_request.updated_at", required=False
        )
        if reopened_at and _insert_event(
            db,
            repo=repo,
            pr_number=number,
            event_type="opened",
            event_at=reopened_at,
            event_key=f"pull:{github_id}:reopened:{reopened_at}",
            actor=None,
            source=source,
            payload={"reopened_at": reopened_at},
        ):
            inserted.append(("opened", reopened_at))

        ordered_reviews = sorted(
            reviews or [],
            key=lambda review: (
                review.get("submitted_at") or "",
                str(review.get("id") or ""),
            ),
        )
        for review in ordered_reviews:
            event_type = REVIEW_STATES.get(str(review.get("state", "")).upper())
            if not event_type:
                continue
            submitted_at = _exact_timestamp(
                review.get("submitted_at"), "review.submitted_at"
            )
            review_id = review.get("id")
            actor = (review.get("user") or {}).get("login")
            if _insert_event(
                db,
                repo=repo,
                pr_number=number,
                event_type=event_type,
                event_at=submitted_at,
                event_key=f"review:{review_id}:{event_type}",
                actor=actor,
                source=source,
                payload={"review_id": review_id, "state": review.get("state")},
            ):
                inserted.append((event_type, submitted_at))

        merged_at = _exact_timestamp(
            pull.get("merged_at"), "pull_request.merged_at", required=False
        )
        closed_at = _exact_timestamp(
            pull.get("closed_at"), "pull_request.closed_at", required=False
        )
        if merged_at:
            if _insert_event(
                db,
                repo=repo,
                pr_number=number,
                event_type="merged",
                event_at=merged_at,
                event_key=f"pull:{github_id}:merged",
                actor=None,
                source=source,
                payload={"merged_at": merged_at},
            ):
                inserted.append(("merged", merged_at))
        elif closed_at or str(pull.get("state", "")).lower() == "closed":
            if not closed_at:
                raise PROutcomeError("closed unmerged PR must include closed_at")
            if _insert_event(
                db,
                repo=repo,
                pr_number=number,
                event_type="closed_unmerged",
                event_at=closed_at,
                event_key=f"pull:{github_id}:closed_unmerged",
                actor=None,
                source=source,
                payload={"closed_at": closed_at},
            ):
                inserted.append(("closed_unmerged", closed_at))
        _upsert_pull(db, repo, pull, source=source, context=context)

    if event_logger:
        for event_type, event_at in inserted:
            event_logger(
                f"pr.{event_type}",
                item_id=context.get("item_id"),
                repo=repo,
                details={
                    "pr_number": number,
                    "github_occurred_at": event_at,
                    "dispatch_id": context.get("dispatch_id"),
                    "project_id": context.get("project_id"),
                    "worker_result_id": context.get("worker_result_id"),
                    "business_acceptance_state": "not_available",
                },
                source=source,
            )
    return {
        "repo": repo,
        "pr_number": number,
        "state": get_pull_request(repo, number, db_path=db_path)["state"],
        "inserted_events": len(inserted),
    }


def ingest_webhook(
    event: str,
    payload: dict[str, Any],
    *,
    db_path: Path | str = OUTCOMES_DB,
    context_resolver: Callable[[str, int], dict[str, Any]] = resolve_context,
    event_logger: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Translate relevant GitHub webhook deliveries into the outcome ledger."""
    repo = (payload.get("repository") or {}).get("full_name")
    pull = payload.get("pull_request") or {}
    action = payload.get("action")
    if event == "pull_request":
        if action not in {"opened", "reopened", "closed"}:
            return {"ignored": True, "inserted_events": 0}
        if action == "reopened":
            pull = {**pull, "_reopened_at": pull.get("updated_at")}
        return ingest_pull_snapshot(
            repo,
            pull,
            db_path=db_path,
            context_resolver=context_resolver,
            source="github_webhook",
            event_logger=event_logger,
        )
    if event == "pull_request_review" and action == "submitted":
        return ingest_pull_snapshot(
            repo,
            pull,
            [payload.get("review") or {}],
            db_path=db_path,
            context_resolver=context_resolver,
            source="github_webhook",
            event_logger=event_logger,
        )
    return {"ignored": True, "inserted_events": 0}


def _duration_seconds(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    first = datetime.fromisoformat(start.replace("Z", "+00:00"))
    last = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return int((last - first).total_seconds())


def get_pull_request(
    repo: str,
    pr_number: int,
    *,
    db_path: Path | str = OUTCOMES_DB,
) -> dict[str, Any] | None:
    with get_db(db_path) as db:
        row = db.execute(
            "SELECT * FROM pull_requests WHERE repo = ? AND pr_number = ?",
            (repo, pr_number),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        reviews = db.execute(
            """
            SELECT event_type, event_at FROM pr_outcome_events
            WHERE repo = ? AND pr_number = ?
              AND event_type IN ('approved', 'changes_requested')
            ORDER BY event_at, id
            """,
            (repo, pr_number),
        ).fetchall()
    result["review_cycles"] = sum(
        review["event_type"] == "changes_requested" for review in reviews
    )
    result["first_review_at"] = reviews[0]["event_at"] if reviews else None
    result["review_delay_seconds"] = _duration_seconds(
        result["opened_at"], result["first_review_at"]
    )
    result["time_to_merge_seconds"] = _duration_seconds(
        result["opened_at"], result["merged_at"]
    )
    return result


def build_report(*, db_path: Path | str = OUTCOMES_DB) -> dict[str, Any]:
    with get_db(db_path) as db:
        keys = db.execute(
            "SELECT repo, pr_number FROM pull_requests ORDER BY repo, pr_number"
        ).fetchall()
        historical = {
            row["event_type"]: row["pull_count"]
            for row in db.execute(
                """
                SELECT event_type, COUNT(DISTINCT repo || '#' || pr_number) AS pull_count
                FROM pr_outcome_events
                GROUP BY event_type
                """
            ).fetchall()
        }
    pulls = [
        get_pull_request(row["repo"], row["pr_number"], db_path=db_path)
        for row in keys
    ]
    counts = {
        state: sum(pull["state"] == state for pull in pulls)
        for state in sorted(CURRENT_STATES)
    }
    tracked = len(pulls)
    covered = sum(pull["state"] in CURRENT_STATES for pull in pulls)
    merge_times = [
        pull["time_to_merge_seconds"]
        for pull in pulls
        if pull["time_to_merge_seconds"] is not None
    ]
    review_delays = [
        pull["review_delay_seconds"]
        for pull in pulls
        if pull["review_delay_seconds"] is not None
    ]
    return {
        "coverage": {
            "tracked": tracked,
            "with_current_or_terminal_state": covered,
            "percent": round(covered / tracked * 100, 1) if tracked else 100.0,
        },
        "conversion": {
            "tracked": tracked,
            "opened": tracked,
            "approved": historical.get("approved", 0),
            "changes_requested": historical.get("changes_requested", 0),
            "merged": counts["merged"],
            "closed_unmerged": counts["closed_unmerged"],
            "open_to_merge_rate": (
                round(counts["merged"] / tracked, 4) if tracked else None
            ),
        },
        "current_states": counts,
        "cycle_times": {
            "time_to_merge_seconds": merge_times,
            "review_delay_seconds": review_delays,
            "review_cycles": [pull["review_cycles"] for pull in pulls],
        },
        "business_acceptance": {
            "state": "not_available",
            "reason": "PR engineering state is not business acceptance",
        },
        "pull_requests": pulls,
    }


class GitHubCLI:
    """Small injectable GitHub client used only by the explicit backfill CLI."""

    def _api(self, endpoint: str) -> list[dict[str, Any]]:
        completed = subprocess.run(
            ["gh", "api", "--paginate", "--slurp", endpoint],
            check=True,
            capture_output=True,
            text=True,
        )
        documents = json.loads(completed.stdout)
        rows: list[dict[str, Any]] = []
        for document in documents:
            rows.extend(document if isinstance(document, list) else [document])
        return rows

    def list_pulls(self, repo: str, since: str) -> list[dict[str, Any]]:
        pulls = self._api(
            f"/repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100"
        )
        cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return [
            pull for pull in pulls
            if datetime.fromisoformat(
                str(pull["updated_at"]).replace("Z", "+00:00")
            ) >= cutoff
        ]

    def list_reviews(self, repo: str, number: int) -> list[dict[str, Any]]:
        return self._api(f"/repos/{repo}/pulls/{number}/reviews?per_page=100")


def backfill_30_days(
    repos: list[str],
    github: Any,
    *,
    db_path: Path | str = OUTCOMES_DB,
    now: datetime | None = None,
    context_resolver: Callable[[str, int], dict[str, Any]] = resolve_context,
) -> dict[str, Any]:
    """Re-read a rolling 30-day window; per-event uniqueness makes restarts safe."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise PROutcomeError("backfill clock must be timezone-aware")
    since = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
    processed = 0
    inserted_events = 0
    for repo in repos:
        with get_db(db_path) as db:
            db.execute(
                """
                INSERT INTO pr_backfill_runs
                  (repo, window_started_at, status, updated_at)
                VALUES (?, ?, 'running', ?)
                ON CONFLICT(repo) DO UPDATE SET
                  window_started_at=excluded.window_started_at,
                  status='running',
                  updated_at=excluded.updated_at
                """,
                (repo, since, _now_iso()),
            )
        for pull in github.list_pulls(repo, since):
            outcome = ingest_pull_snapshot(
                repo,
                pull,
                github.list_reviews(repo, pull["number"]),
                db_path=db_path,
                context_resolver=context_resolver,
                source="github_backfill",
            )
            processed += 1
            inserted_events += outcome["inserted_events"]
            with get_db(db_path) as db:
                db.execute(
                    """
                    UPDATE pr_backfill_runs
                    SET last_pr_number = ?, updated_at = ?
                    WHERE repo = ?
                    """,
                    (pull["number"], _now_iso(), repo),
                )
        with get_db(db_path) as db:
            db.execute(
                """
                UPDATE pr_backfill_runs
                SET status = 'completed', last_completed_at = ?, updated_at = ?
                WHERE repo = ?
                """,
                (_now_iso(), _now_iso(), repo),
            )
    return {
        "window_started_at": since,
        "processed": processed,
        "inserted_events": inserted_events,
        "report": build_report(db_path=db_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", action="append", required=True)
    parser.add_argument("--db", type=Path, default=OUTCOMES_DB)
    args = parser.parse_args()
    print(
        json.dumps(
            backfill_30_days(args.repo, GitHubCLI(), db_path=args.db),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
