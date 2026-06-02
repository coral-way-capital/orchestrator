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
- **Lint is mandatory** — after making all changes, you MUST run the project's linter and type checker (e.g. `bun run lint`, `bun run typecheck`, `biome check`, `tsc --noEmit`). If YOUR changes introduce lint/type errors, fix them before committing. Do NOT skip this step.
- **Pre-existing errors**: If `git commit` fails due to errors in files you did NOT modify, use `git commit --no-verify`. But if the errors are in YOUR files, fix them first.
- **Verify before commit**: Run `git diff --name-only` to see what you changed, then run lint/typecheck. Only commit when your files are clean.
- If you encounter blockers, document them in the PR body.
- **Finish the job**: You MUST commit, push, and create a PR. Uncommitted work on a local branch is a failure state. If you can't open a PR, explain why in a GitHub issue comment using `gh issue comment`.
