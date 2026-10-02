import contextlib
import hashlib
import hmac
import http.client
from http.server import HTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import factory_bridge as factory


def issue(number=7, **overrides):
    return {"number": number, "title": "Fix the behavior", "state": "open",
            "assignees": [{"login": "ivanacostarubio"}], "labels": [], **overrides}


class FactoryTests(unittest.TestCase):
    def test_script_only_guardian_is_silent_when_idle_and_keeps_failures_visible(self):
        from factory.pr_guardian_job import main
        reports = [(0, {"merged": [], "errors": []}, ""),
                   (1, {"merged": [], "errors": ["fixture failure"]}, "fixture failure"),
                   (0, {"merged": [{"repo": "fixture", "number": 3}], "errors": []}, "fixture")]
        with patch("sys.argv", ["pr-guardian.py", "--dry-run"]):
            for code, report, expected in reports:
                result = Mock(returncode=code, stdout="report\n---JSON---\n" + json.dumps(report))
                output = io.StringIO()
                with patch("subprocess.run", return_value=result) as run, contextlib.redirect_stdout(output):
                    self.assertEqual(main(), code)
                self.assertEqual(run.call_args.args[0][-1], "--dry-run")
                if expected:
                    self.assertIn(expected, output.getvalue())
                else:
                    self.assertEqual(output.getvalue(), "")
            with patch("subprocess.run", return_value=Mock(returncode=0, stdout="unparseable")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(), 1)

    def test_admission_preserves_exclusions_and_fails_closed_on_missing_github_evidence(self):
        with patch.dict(os.environ, {"CWC_ISSUE_ASSIGNEES": "ivanacostarubio"}, clear=False):
            factory.allowed_assignees.cache_clear()
            for repo, payload in [
                ("coral-way-capital/visit-merida-chatbot", issue()),
                ("coral-way-capital/orchestrator", issue(assignees=[])),
                ("coral-way-capital/orchestrator", issue(state="closed")),
                ("coral-way-capital/orchestrator", issue(labels=[{"name": "docs"}])),
                ("coral-way-capital/orchestrator", issue(title="[Parent #3] Child")),
            ]:
                with self.subTest(repo=repo, payload=payload):
                    self.assertIsNotNone(factory.eligibility_reason(repo, payload))
            self.assertIsNone(factory.eligibility_reason("coral-way-capital/orchestrator", issue()))
            board = Mock()
            board.list_tasks.return_value = []
            with patch.object(factory, "gh", side_effect=RuntimeError("API unavailable")), patch.object(factory, "workspace") as workspace:
                with self.assertRaises(RuntimeError):
                    factory.intake(board, object(), "coral-way-capital/orchestrator", issue())
                board.create_task.assert_not_called()
                workspace.assert_not_called()
            pages = [[{"source": {"issue": {"state": "closed", "pull_request": {"url": "old"}}}}],
                     [{"source": {"issue": {"state": "open", "pull_request": {"url": "current"}}}}]]
            with patch.object(factory, "gh", return_value=pages):
                self.assertTrue(factory.linked_pr("coral-way-capital/orchestrator", 7))
        factory.allowed_assignees.cache_clear()

    def test_retired_http_controls_cannot_dispatch_and_signed_intake_has_retriable_failure(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            root = Path(tmp)
            import events
            stack.enter_context(patch.object(events, "DB_PATH", root / "events.db"))
            import issue_queue_db
            for name, value in {"BASE_DIR": root, "DB_FILE": root / "queue.db", "QUEUE_FILE": root / "queue.json"}.items():
                stack.enter_context(patch.object(issue_queue_db, name, value))
            import webhook_receiver as receiver
            stack.enter_context(patch.dict(os.environ, {"CWC_FACTORY_BACKEND": "kanban"}))
            stack.enter_context(patch.object(receiver, "load_secret", return_value="fixture-only"))
            stack.enter_context(patch.object(receiver.IssueWebhookHandler, "log_message"))
            retired = stack.enter_context(patch.object(receiver.IssueWebhookHandler, "_dispatch_agent"))
            bridge = stack.enter_context(patch.object(receiver.subprocess, "run", return_value=Mock(returncode=0)))
            server = HTTPServer(("127.0.0.1", 0), receiver.IssueWebhookHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, body=b"{}", headers=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                try:
                    connection.request(method, path, body=body, headers=headers or {})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()
            try:
                for method, path in [("POST", "/api/dispatch"), ("POST", "/api/finish"),
                                     ("PATCH", "/api/queue/retry/example"), ("GET", "/api/sync"),
                                     ("GET", "/api/health"), ("GET", "/api/agent-status")]:
                    self.assertEqual(request(method, path)[0], 410)
                payload = json.dumps({"action": "opened", "issue": issue(), "repository": {"full_name": "coral-way-capital/orchestrator"}}).encode()
                headers = {"X-GitHub-Event": "issues", "Content-Type": "application/json"}
                self.assertEqual(request("POST", "/", payload, headers)[0], 403)
                bridge.assert_not_called()
                headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(b"fixture-only", payload, hashlib.sha256).hexdigest()
                self.assertEqual(request("POST", "/", payload, headers)[0], 200)
                self.assertEqual(bridge.call_args.kwargs["input"], payload)
                bridge.return_value.returncode = 1
                self.assertEqual(request("POST", "/", payload, headers)[0], 503)
                retired.assert_not_called()
            finally:
                server.shutdown()
                thread.join()
                server.server_close()

    @unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "Native integration runs on Maya's installed Hermes")
    def test_native_board_deduplicates_serializes_repos_and_preserves_review_handoff(self):
        from hermes_cli import kanban_db as kb
        from hermes_cli.kanban_db_connect import connect
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"HERMES_KANBAN_HOME": tmp, "CWC_ISSUE_ASSIGNEES": "ivanacostarubio"}))
            factory.allowed_assignees.cache_clear()
            stack.enter_context(patch.object(factory, "linked_pr", return_value=False))
            stack.enter_context(patch.object(factory, "outcome_contract_for_repo", return_value=None))
            stack.enter_context(patch.object(factory, "workspace", side_effect=lambda repo, n: (tmp, f"factory/{n}")))
            kb.create_board("factory")
            conn = connect(board="factory")
            stack.callback(conn.close)
            one = factory.intake(kb, conn, "coral-way-capital/alpha", issue(1))
            self.assertIsNone(factory.intake(kb, conn, "coral-way-capital/alpha", issue(1)))
            two = factory.intake(kb, conn, "coral-way-capital/alpha", issue(2))
            other = factory.intake(kb, conn, "coral-way-capital/beta", issue(1))
            self.assertEqual(len(kb.list_tasks(conn)), 3)
            self.assertEqual(kb.get_task(conn, one).status, "ready")
            self.assertEqual(kb.get_task(conn, two).status, "todo")
            self.assertEqual(kb.get_task(conn, other).status, "ready")
            self.assertTrue(kb.request_review(conn, one, summary="Exact head and tests", reviewer="factory-reviewer"))
            reviewed = kb.get_task(conn, one)
            self.assertEqual((reviewed.status, reviewed.assignee), ("review", "factory-reviewer"))
            self.assertEqual(kb.get_task(conn, two).status, "todo")
            # Real native acceptance gate: unavailable GitHub evidence must not mark done.
            with patch("hermes_cli.kanban_pr_acceptance._api", side_effect=OSError("offline")):
                self.assertFalse(kb.complete_task(conn, one, summary="Reviewed", metadata={"published_pr": "https://github.com/coral-way-capital/alpha/pull/5"}))
            self.assertEqual(kb.get_task(conn, one).status, "review")
            factory.intake(kb, conn, "coral-way-capital/beta", issue(1, assignees=[]))
            self.assertEqual(kb.get_task(conn, other).status, "blocked")
            historical = factory.intake(kb, conn, "coral-way-capital/visit-merida-chatbot", issue(), legacy_status="pending")
            self.assertEqual(kb.get_task(conn, historical).status, "blocked")
            completed = factory.intake(kb, conn, "coral-way-capital/history", issue(1), legacy_status="completed")
            self.assertEqual(kb.get_task(conn, completed).status, "done")
            failed = factory.intake(kb, conn, "coral-way-capital/history", issue(2), legacy_status="failed")
            self.assertEqual(kb.get_task(conn, failed).status, "blocked")
            fresh = factory.intake(kb, conn, "coral-way-capital/history", issue(3))
            self.assertEqual(kb.get_task(conn, fresh).status, "ready", "inert history must not acquire a repository lane")
        factory.allowed_assignees.cache_clear()


if __name__ == "__main__":
    unittest.main()
