"""Execute the published workspace recipes against disposable local Git repos."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parent


class WorkspacePromptTests(unittest.TestCase):
    def test_parallel_repositories_and_retries_preserve_existing_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            subprocess.run(["git", "init", "-q", str(origin)], check=True)
            (origin / "tracked.txt").write_text("original\n")
            subprocess.run(["git", "-C", str(origin), "add", "tracked.txt"], check=True)
            subprocess.run(["git", "-C", str(origin), "-c", "user.name=Test", "-c",
                            "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True)
            sha = subprocess.check_output(["git", "-C", str(origin), "rev-parse", "HEAD"], text=True).strip()
            repos = {}
            for name in ("alpha", "beta"):
                repo = root / name
                subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
                (repo / "tracked.txt").write_text("unfinished shared edit\n")
                repos[name] = repo
            workspaces = []
            recipes = [("cwc-issue-dispatch", "alpha"), ("cwc-issue-dispatch", "beta"),
                       ("cwc-issue-dispatch", "alpha"), ("cwc-pr-review", "alpha"),
                       ("cwc-pr-review-cycle", "alpha"), ("cwc-pr-gap-check", "alpha"),
                       ("cwc-ac-verify", "alpha")]
            for recipe, name in recipes:
                text = (ROOT / "webhooks/prompts" / f"{recipe}.md").read_text()
                script = next(block for block in re.findall(r"```(?:bash)?\n(.*?)\n```", text, re.S)
                              if "git worktree add" in block or "worktree add" in block)
                script = script.replace("{local_path}", str(repos[name]))
                script = script.replace("/home/deploy/apps/{repository.name}", str(repos[name]))
                script = script.replace("/tmp/cwc-work-{issue.number}", str(root / "legacy-work"))
                for key, value in {"repository.full_name": f"example/{name}", "repository.name": name,
                                   "issue.number": "7", "pull_request.number": "7",
                                   "branch": f"fix/example-{name}-issue-7-run-{len(workspaces)}",
                                   "worktree": str(root / "profile" / "workspaces" / f"example-{name}-issue-7-run-{len(workspaces)}" / "worktree"),
                                   "pull_request.head.sha": sha, "pull_request.base.sha": sha}.items():
                    script = script.replace("{" + key + "}", value)
                env = {**os.environ, "HERMES_HOME": str(root / "profile")}
                result = subprocess.run(["bash", "-c", script + "\npwd\n"], env=env,
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                workspace = Path(result.stdout.strip().splitlines()[-1])
                self.assertNotIn(workspace, workspaces)
                (workspace / "unfinished.txt").write_text(recipe)
                workspaces.append(workspace)
                for previous in workspaces:
                    self.assertTrue((previous / "unfinished.txt").exists())
                for repo in repos.values():
                    self.assertEqual((repo / "tracked.txt").read_text(), "unfinished shared edit\n")
            self.assertEqual(len(workspaces), 7)


if __name__ == "__main__":
    unittest.main()
