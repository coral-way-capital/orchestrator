#!/usr/bin/env python3
"""Deterministic integration fixtures for PR engineering outcomes (issue #17)."""
from __future__ import annotations

import json
import atexit
import hashlib
import hmac
import http.client
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

_TEST_HOME = tempfile.mkdtemp(prefix="orchestrator-pr-outcomes-")
os.environ["HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)

import pr_outcomes
import webhook_receiver


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "pr_outcomes.json").read_text(encoding="utf-8")
)
REPO = FIXTURE["repository"]["full_name"]


def context_resolver(repo, pr_number):
    if pr_number != 17:
        return {
            "item_id": None,
            "dispatch_id": None,
            "project_id": None,
            "worker_result_id": None,
            "linkage_state": "not_available",
        }
    return {
        "item_id": "coral-way-capital/cwc-control-plane#17",
        "dispatch_id": "webhook:cwc-issue-dispatch:issue-17",
        "project_id": "mission-control",
        "worker_result_id": 1517,
        "linkage_state": "linked",
    }


def test_webhook_states_are_idempotent_and_correlated():
    pull = dict(FIXTURE["pull_requests"][0])
    pull.update({"state": "open", "closed_at": None, "merged_at": None})
    events = []
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        payload = {"action": "opened", "repository": FIXTURE["repository"], "pull_request": pull}
        first = pr_outcomes.ingest_webhook(
            "pull_request", payload, db_path=db, context_resolver=context_resolver,
            event_logger=lambda *args, **kwargs: events.append((args, kwargs)),
        )
        duplicate = pr_outcomes.ingest_webhook(
            "pull_request", payload, db_path=db, context_resolver=context_resolver,
            event_logger=lambda *args, **kwargs: events.append((args, kwargs)),
        )
        row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)

        assert first["inserted_events"] == 1
        assert duplicate["inserted_events"] == 0
        assert len(events) == 1
        assert row["state"] == "opened"
        assert row["opened_at"] == "2026-07-01T10:00:00Z"
        assert row["item_id"] == "coral-way-capital/cwc-control-plane#17"
        assert row["dispatch_id"] == "webhook:cwc-issue-dispatch:issue-17"
        assert row["project_id"] == "mission-control"
        assert row["worker_result_id"] == 1517
        assert row["linkage_state"] == "linked"
        assert row["business_acceptance_state"] == "not_available"

        for review, expected_state in (
            (FIXTURE["reviews"]["17"][0], "changes_requested"),
            (FIXTURE["reviews"]["17"][1], "approved"),
        ):
            review_payload = {
                "action": "submitted",
                "repository": FIXTURE["repository"],
                "pull_request": pull,
                "review": review,
            }
            pr_outcomes.ingest_webhook(
                "pull_request_review",
                review_payload,
                db_path=db,
                context_resolver=context_resolver,
            )
            assert (
                pr_outcomes.get_pull_request(REPO, 17, db_path=db)["state"]
                == expected_state
            )


def test_concurrent_duplicate_delivery_inserts_one_event():
    pull = dict(FIXTURE["pull_requests"][0])
    pull.update({"state": "open", "closed_at": None, "merged_at": None})
    payload = {
        "action": "opened",
        "repository": FIXTURE["repository"],
        "pull_request": pull,
    }
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        barrier = threading.Barrier(8)
        outcomes = []
        errors = []

        def deliver():
            try:
                barrier.wait()
                outcomes.append(
                    pr_outcomes.ingest_webhook(
                        "pull_request",
                        payload,
                        db_path=db,
                        context_resolver=context_resolver,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=deliver) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors, errors
        assert sum(outcome["inserted_events"] for outcome in outcomes) == 1
        with sqlite3.connect(db) as outcome_db:
            event_count = outcome_db.execute(
                "SELECT COUNT(*) FROM pr_outcome_events"
            ).fetchone()[0]
        assert event_count == 1


def test_reopened_pr_can_close_again_idempotently():
    pull = dict(FIXTURE["pull_requests"][0])
    pull.update({"state": "open", "closed_at": None, "merged_at": None})
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"

        first_close = {
            **pull,
            "state": "closed",
            "updated_at": "2026-07-02T09:00:00Z",
            "closed_at": "2026-07-02T09:00:00Z",
        }
        reopened = {
            **pull,
            "updated_at": "2026-07-03T09:00:00Z",
        }
        second_close = {
            **pull,
            "state": "closed",
            "updated_at": "2026-07-04T09:00:00Z",
            "closed_at": "2026-07-04T09:00:00Z",
        }
        for action, snapshot in (
            ("closed", first_close),
            ("reopened", reopened),
            ("closed", second_close),
        ):
            pr_outcomes.ingest_webhook(
                "pull_request",
                {
                    "action": action,
                    "repository": FIXTURE["repository"],
                    "pull_request": snapshot,
                },
                db_path=db,
                context_resolver=context_resolver,
            )
            if action == "reopened":
                reopened_row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)
                assert reopened_row["state"] == "opened"
                assert reopened_row["closed_at"] is None

        row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)
        assert row["state"] == "closed_unmerged"
        assert row["closed_at"] == "2026-07-04T09:00:00Z"

        replayed_old_close = pr_outcomes.ingest_webhook(
            "pull_request",
            {
                "action": "closed",
                "repository": FIXTURE["repository"],
                "pull_request": first_close,
            },
            db_path=db,
            context_resolver=context_resolver,
        )
        row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)
        assert replayed_old_close["inserted_events"] == 0
        assert row["state"] == "closed_unmerged"
        assert row["closed_at"] == "2026-07-04T09:00:00Z"

        duplicate = pr_outcomes.ingest_webhook(
            "pull_request",
            {
                "action": "closed",
                "repository": FIXTURE["repository"],
                "pull_request": second_close,
            },
            db_path=db,
            context_resolver=context_resolver,
        )
        assert duplicate["inserted_events"] == 0


def test_dismissed_review_no_longer_sets_current_state():
    pull = dict(FIXTURE["pull_requests"][0])
    pull.update({"state": "open", "closed_at": None, "merged_at": None})
    review = dict(FIXTURE["reviews"]["17"][1])
    deliveries = (("submitted", "APPROVED"), ("dismissed", "DISMISSED"))
    for ordered_deliveries in (deliveries, tuple(reversed(deliveries))):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "pr-outcomes.db"
            for action, state in ordered_deliveries:
                pr_outcomes.ingest_webhook(
                    "pull_request_review",
                    {
                        "action": action,
                        "repository": FIXTURE["repository"],
                        "pull_request": pull,
                        "review": {**review, "state": state},
                    },
                    db_path=db,
                    context_resolver=context_resolver,
                )
            row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)
            assert row["state"] == "opened"
            assert row["review_cycles"] == 0


def test_approval_does_not_clear_another_reviewers_change_request():
    pull = dict(FIXTURE["pull_requests"][0])
    pull.update({"state": "open", "closed_at": None, "merged_at": None})
    reviews = [
        {
            "id": 901,
            "state": "CHANGES_REQUESTED",
            "submitted_at": "2026-07-01T12:00:00Z",
            "user": {"login": "reviewer-one"},
        },
        {
            "id": 902,
            "state": "APPROVED",
            "submitted_at": "2026-07-01T13:00:00Z",
            "user": {"login": "reviewer-two"},
        },
    ]
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        pr_outcomes.ingest_pull_snapshot(
            REPO,
            pull,
            list(reversed(reviews)),
            db_path=db,
            context_resolver=context_resolver,
        )
        assert (
            pr_outcomes.get_pull_request(REPO, 17, db_path=db)["state"]
            == "changes_requested"
        )


def test_review_cycles_and_merge_time_use_exact_github_timestamps():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        pr_outcomes.ingest_pull_snapshot(
            REPO,
            FIXTURE["pull_requests"][0],
            FIXTURE["reviews"]["17"],
            db_path=db,
            context_resolver=context_resolver,
        )
        row = pr_outcomes.get_pull_request(REPO, 17, db_path=db)
        report = pr_outcomes.build_report(db_path=db)

        assert row["state"] == "merged"
        assert row["merged_at"] == "2026-07-03T15:30:00Z"
        assert row["review_cycles"] == 2
        assert row["first_review_at"] == "2026-07-01T12:00:00Z"
        assert row["review_delay_seconds"] == 7200
        assert row["time_to_merge_seconds"] == 192600
        assert report["conversion"]["tracked"] == 1
        assert report["conversion"]["merged"] == 1
        assert report["business_acceptance"]["state"] == "not_available"
        assert report["pull_requests"][0]["business_acceptance_state"] == "not_available"


def test_backfill_is_restart_safe_and_every_pull_has_a_state():
    class FakeGitHub:
        def __init__(self, expected_since="2026-06-27T12:00:00Z"):
            self.expected_since = expected_since

        def list_pulls(self, repo, since):
            assert repo == REPO
            assert since == self.expected_since
            return FIXTURE["pull_requests"]

        def list_reviews(self, repo, number):
            return FIXTURE["reviews"][str(number)]

    class InterruptingGitHub(FakeGitHub):
        def list_reviews(self, repo, number):
            if number == 18:
                raise RuntimeError("simulated interruption")
            return super().list_reviews(repo, number)

    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        try:
            pr_outcomes.backfill_30_days(
                [REPO], InterruptingGitHub(), db_path=db, now=now,
                context_resolver=context_resolver,
            )
        except RuntimeError as exc:
            assert str(exc) == "simulated interruption"
        else:
            raise AssertionError("fixture must interrupt the first backfill")
        assert pr_outcomes.get_pull_request(REPO, 17, db_path=db)["state"] == "merged"
        with sqlite3.connect(db) as checkpoint_db:
            checkpoint = checkpoint_db.execute(
                """
                SELECT status, last_pr_number
                FROM pr_backfill_runs WHERE repo = ?
                """,
                (REPO,),
            ).fetchone()
        assert checkpoint == ("failed", 17)

        second = pr_outcomes.backfill_30_days(
            [REPO], FakeGitHub(), db_path=db, now=now + timedelta(days=1),
            context_resolver=context_resolver,
        )
        third = pr_outcomes.backfill_30_days(
            [REPO],
            FakeGitHub(expected_since="2026-06-28T12:00:00Z"),
            db_path=db,
            now=now + timedelta(days=1),
            context_resolver=context_resolver,
        )
        report = pr_outcomes.build_report(db_path=db)

        assert second["processed"] == 2
        assert second["inserted_events"] == 2
        assert third["inserted_events"] == 0
        assert len(report["pull_requests"]) == 2
        assert {row["state"] for row in report["pull_requests"]} == {
            "merged", "closed_unmerged"
        }
        assert report["coverage"]["with_current_or_terminal_state"] == 2
        assert report["coverage"]["percent"] == 100.0
        assert report["conversion"]["closed_unmerged"] == 1


def test_input_validation_and_missing_webhook_secret_fail_closed():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        try:
            pr_outcomes.get_pull_request(
                "coral-way-capital/orchestrator?unsafe=true", 17, db_path=db
            )
        except pr_outcomes.PROutcomeError:
            pass
        else:
            raise AssertionError("invalid repository must be rejected")

        try:
            pr_outcomes.get_pull_request(REPO, 10**30, db_path=db)
        except pr_outcomes.PROutcomeError:
            pass
        else:
            raise AssertionError("oversized PR number must be rejected")

    original_secret = webhook_receiver.load_secret
    try:
        webhook_receiver.load_secret = lambda: None
        assert not webhook_receiver.verify_signature(b"{}", "")
    finally:
        webhook_receiver.load_secret = original_secret


def test_signed_github_webhook_reaches_pr_outcome_ledger():
    original_secret = webhook_receiver.load_secret
    original_ingest = webhook_receiver.ingest_pr_webhook
    original_report = webhook_receiver.build_pr_outcomes_report
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "pr-outcomes.db"
        webhook_receiver.load_secret = lambda: "fixture-secret"
        webhook_receiver.ingest_pr_webhook = lambda event, payload, event_logger: (
            pr_outcomes.ingest_webhook(
                event,
                payload,
                db_path=db,
                context_resolver=context_resolver,
                event_logger=event_logger,
            )
        )
        webhook_receiver.build_pr_outcomes_report = lambda: pr_outcomes.build_report(
            db_path=db
        )
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), webhook_receiver.IssueWebhookHandler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            pull = dict(FIXTURE["pull_requests"][0])
            pull.update({"state": "open", "closed_at": None, "merged_at": None})
            payload = {
                "action": "opened",
                "repository": FIXTURE["repository"],
                "pull_request": pull,
            }
            body = json.dumps(payload).encode()
            signature = "sha256=" + hmac.new(
                b"fixture-secret", body, hashlib.sha256
            ).hexdigest()
            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            conn.request(
                "POST",
                "/",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "pull_request",
                    "X-Hub-Signature-256": signature,
                },
            )
            response = conn.getresponse()
            result = json.loads(response.read())
            conn.close()
            assert response.status == 200, result
            assert result["state"] == "opened"
            assert pr_outcomes.get_pull_request(REPO, 17, db_path=db)["state"] == "opened"

            conn = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=2
            )
            conn.request("GET", "/api/pr-outcomes")
            response = conn.getresponse()
            report = json.loads(response.read())
            conn.close()
            assert response.status == 200, report
            assert report["coverage"]["percent"] == 100.0
            assert report["business_acceptance"]["state"] == "not_available"
        finally:
            server.shutdown()
            server.server_close()
            webhook_receiver.load_secret = original_secret
            webhook_receiver.ingest_pr_webhook = original_ingest
            webhook_receiver.build_pr_outcomes_report = original_report


if __name__ == "__main__":
    test_webhook_states_are_idempotent_and_correlated()
    test_concurrent_duplicate_delivery_inserts_one_event()
    test_reopened_pr_can_close_again_idempotently()
    test_dismissed_review_no_longer_sets_current_state()
    test_approval_does_not_clear_another_reviewers_change_request()
    test_review_cycles_and_merge_time_use_exact_github_timestamps()
    test_backfill_is_restart_safe_and_every_pull_has_a_state()
    test_input_validation_and_missing_webhook_secret_fail_closed()
    test_signed_github_webhook_reaches_pr_outcome_ledger()
    print("smoke_pr_outcomes: ok")
