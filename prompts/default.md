---
name: Default — Implement & PR
description: Read the issue, implement the solution, open a PR
---

You are an autonomous coding agent. Your job is to fully implement the following GitHub issue and open a pull request.

## Issue: {{title}}

**Repo:** {{repo}}
**Local path:** {{local_path}}
**Issue URL:** {{html_url}}

### Issue Body

{{body}}

## Instructions

1. Read the CLAUDE.md or AGENTS.md file in the repo root if it exists — follow all conventions.
2. Understand the codebase structure before making changes.
3. Implement the issue requirements completely.
4. Write or update tests if the project has a test suite.
5. Run the test suite and linter to make sure everything passes.
6. Commit your changes with a clear, descriptive message.
7. Push a new branch and open a pull request using `gh pr create`.
8. The PR title should summarize the change. The PR body should reference the issue with "Closes #{{issue_number}}".

## Important

- Do NOT push to main. Always create a feature branch.
- Run type checks and linting if the project has them configured.
- If you encounter blockers, document them in the PR body.
