import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

class DispatchWorkspaceTests(unittest.TestCase):
    def test_dispatch_payload_queue_and_recovery_share_unique_workspace_identity(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.ExitStack() as stack:
            root = Path(temporary)
            # Redirect import-time event initialization and the queue's SQLite mirror.
            import events
            stack.enter_context(patch.object(events, "DB_PATH", root / "events.db"))
            import issue_queue_db
            stack.enter_context(patch.object(issue_queue_db, "BASE_DIR", root))
            stack.enter_context(patch.object(issue_queue_db, "DB_FILE", root / "queue-state.db"))
            stack.enter_context(patch.object(issue_queue_db, "QUEUE_FILE", root / "queue.json"))
            import webhook_receiver as receiver
            from worker_pools import WorkerPoolsManager
            pools = WorkerPoolsManager(str(root / "pools.json"))
            stack.enter_context(patch.dict(os.environ, {"HERMES_HOME": str(root / "profile")}))
            stack.enter_context(patch.object(receiver._issue_queue, "QUEUE_FILE", root / "queue.json"))
            stack.enter_context(patch.object(receiver, "_pool_mgr", pools))
            stack.enter_context(patch.object(receiver, "issue_queue_db", None))
            for name in ["log_event", "upsert_trace"]:
                stack.enter_context(patch.object(receiver, name))
            stack.enter_context(patch.object(receiver, "outcome_contract_for_repo", return_value=None))
            stack.enter_context(patch.object(receiver, "load_gateway_secret", return_value="fixture-secret"))
            paths = {"meta_json": root / "meta.json", "prompt_md": root / "prompt.md", "trace_dir": root}
            stack.enter_context(patch.object(receiver, "ensure_trace_bundle", return_value=paths))
            response = MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b"{}"
            response.getcode.return_value = 202
            gateway = stack.enter_context(patch.object(receiver, "urlopen", return_value=response))
            receiver._issue_queue.save_queue({
                "pending": [{"id": f"example/{name}#7", "repo": f"example/{name}", "issue_number": 7}
                            for name in ["alpha", "beta"]],
                "in_progress": [], "completed": [], "failed": []})
            handler = receiver.IssueWebhookHandler.__new__(receiver.IssueWebhookHandler)
            previous = []
            for name in ["alpha", "beta", "alpha"]:
                repo, item_id = f"example/{name}", f"example/{name}#7"
                result, status = handler._dispatch_agent(item_id, repo, 7, "Fixture", "", "", "default")
                self.assertEqual(status, 202, result)
                payload = json.loads(gateway.call_args.args[0].data)
                self.assertIn("worktree", payload, "dispatch must provide the workspace it records")
                self.assertIn("branch", payload)
                self.assertNotIn(payload["worktree"], previous)
                self.assertTrue(payload["branch"].startswith(f"fix/example-{name}-issue-7-"))
                self.assertTrue(Path(payload["worktree"]).is_relative_to(root / "profile" / "workspaces"))
                queued = next(i for i in receiver._issue_queue.load_queue()["in_progress"] if i["id"] == item_id)
                worker = next(i for i in json.loads((root / "pools.json").read_text())["workers"] if i["item_id"] == item_id)
                meta = json.loads(paths["meta_json"].read_text())
                for record in [queued, worker, meta]:
                    self.assertEqual(record["worktree"], payload["worktree"])
                    self.assertEqual(record["branch"], payload["branch"])
                Path(payload["worktree"]).mkdir(parents=True)
                (Path(payload["worktree"]) / "unfinished.txt").write_text("recover me")
                previous.append(payload["worktree"])
                self.assertTrue(all((Path(path) / "unfinished.txt").exists() for path in previous))


if __name__ == "__main__":
    unittest.main()
