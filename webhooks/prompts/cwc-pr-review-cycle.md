A human reviewer submitted a review on PR #{pull_request.number} in {repository.full_name}.

PR #{pull_request.number}: {pull_request.title}
Repo: {repository.full_name}
Branch: {pull_request.head.ref} -> {pull_request.base.ref}
Head SHA: {pull_request.head.sha}
Review state: {review.state}
Reviewer: {review.user.login}

STEP 0 - GATE CHECKS
a) Get your bot username: BOT=$(gh api user --jq '.login')
b) If the reviewer ({review.user.login}) IS your username, return an empty string. This handler is for HUMAN reviews only.
c) If review.state is "approved" or "commented", return an empty string. Only act on "changes_requested".
d) Count your previous fix-cycles on this PR:
   gh api repos/{repository.full_name}/pulls/{pull_request.number}/reviews --paginate --jq '[.[] | select(.user.login == "'"'$BOT'"'" and (.body | test("Re-review cycle")))] | length'
e) If count >= 5, post a PR comment: "Review-fix cycle limit reached (5/5). Requires human review." then return empty.

WORKSPACE PREPARATION
```bash
set -e
REPO_DIR='/home/deploy/apps/{repository.name}'
REPO_KEY=$(printf '%s' '{repository.full_name}' | tr '/' '-')
RUN_ROOT="${HERMES_HOME:-$HOME/.hermes}/workspaces"
mkdir -p "$RUN_ROOT"
RUN_DIR=$(mktemp -d "$RUN_ROOT/$REPO_KEY-pr-{pull_request.number}-fix-XXXXXX")
if [ ! -d "$REPO_DIR/.git" ] && [ ! -f "$REPO_DIR/.git" ]; then
  REPO_DIR="$RUN_DIR/repository"
  git clone --no-checkout 'https://github.com/{repository.full_name}.git' "$REPO_DIR"
fi
git -C "$REPO_DIR" fetch --no-tags origin '{pull_request.head.sha}' '{pull_request.base.sha}'
git -C "$REPO_DIR" worktree add --detach "$RUN_DIR/worktree" '{pull_request.head.sha}'
cd "$RUN_DIR/worktree"
test "$(git rev-parse HEAD)" = '{pull_request.head.sha}'
```

Record `RUN_DIR` and `REPO_DIR` and reuse those exact values throughout this run.
Keep payloads in `RUN_DIR`, outside the code checkout. Never stash, switch,
reset, or clean the shared checkout, or delete a previous run's worktree/branch.
If checkout/fetch fails, stop; do not test or review a different commit.
Before posting review/AC evidence, verify the live PR head equals the reviewed
commit. Before pushing fixes, verify the live PR head equals the starting commit
of that fix cycle; push normally and update that expected head after success.
Stop on a changed head or a rejected push; never force push. If a fix requires
writing to a fork, report that blocker instead of pushing to a same-named
branch in the base repository.

STEP 1 - GATHER UNRESOLVED FEEDBACK
Write query to "$RUN_DIR/threads_query.json":
{
  "query": "query($owner: String!, $repo: String!, $pr: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $pr) { reviewThreads(first: 50) { nodes { id isResolved isOutdated comments(first: 10) { nodes { id databaseId author { login } body path line } } } } } }",
  "variables": {"owner": "{repository.owner.login}", "repo": "{repository.name}", "pr": {pull_request.number}}
}
Run: gh api graphql --input "$RUN_DIR/threads_query.json"

Collect UNRESOLVED + NON-OUTDATED threads from the human reviewer only. Deduplicate by file:line.

STEP 2 - CHECK OUT AND FIX
Use the isolated `$RUN_DIR/worktree` prepared above.
f) For each unresolved actionable thread:
   1. Read the file at the referenced path and line
   2. Understand the feedback
   3. Implement the fix
   4. Resolve the thread via GraphQL:
      Write to "$RUN_DIR/resolve_thread.json":
      {"query": "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } } }", "variables": {"threadId": "<THREAD_ID>"}}
      Run: gh api graphql --input "$RUN_DIR/resolve_thread.json"
   5. Verify resolution succeeded

STEP 3 - VERIFY AND PUSH
a) bunx tsc --noEmit (if TypeScript files changed)
b) Run targeted tests on affected files only (use ././ prefix for file-path mode in bun test)
c) git add -- <files changed for this review> (inspect the diff first)
d) git commit -m "fix: address human review feedback (cycle N/5)"
e) PR_BRANCH=$(gh pr view {pull_request.number} --repo {repository.full_name} --json headRefName --jq .headRefName); git push origin "HEAD:refs/heads/$PR_BRANCH"

STEP 4 - RE-REVIEW
a) Wait 10 seconds for GitHub to process the push
b) Get new HEAD SHA: git rev-parse HEAD
c) Get updated diff: git diff '{pull_request.base.sha}'...HEAD
d) Re-review with full rigor (correctness, security, quality, testing, performance)
e) Submit review via Reviews API:
   Write review JSON to "$RUN_DIR/review.json" with new HEAD SHA
   Run: gh api repos/{repository.full_name}/pulls/{pull_request.number}/reviews --input "$RUN_DIR/review.json"
   event: "COMMENT" (bot is PR author)
   If CLEAN: body = "Re-review cycle N/5: APPROVED"
   If issues remain: body = "Re-review cycle N/5: Y issues remain" with inline comments
f) If Reviews API drops inline comments, post individually via:
   POST repos/{repository.full_name}/pulls/{pull_request.number}/comments

STEP 5 - CLEANUP
Preserve the run directory and record its path on failure or unfinished work.
After a successful review/push, optional cleanup may remove only this run's
clean worktree: `git -C "$REPO_DIR" worktree remove "$RUN_DIR/worktree"`.
Do not use force; if removal fails, leave the worktree for recovery. Never
switch the shared checkout or delete shared/local branches during cleanup.
Return the fix/review result and any recovery path.

RULES:
- Only process reviews from HUMAN reviewers, not your own bot reviews
- Always resolve threads via GraphQL for issues you have addressed
- Always write GraphQL queries and review payloads as JSON files (--input) to avoid shell escaping
- Do NOT sign reviews or add any attribution
- Do NOT post additional PR comments beyond the formal reviews
- Do NOT narrate your process
- Do NOT run the self-heal step or check for noise comments
- Compare test results against the BASE branch ({pull_request.base.ref}), not main
- If git operations fail, describe the error briefly and stop
- If no shared clone exists, use the private clone created inside RUN_DIR.
- Use the github-code-review skill for the review methodology and pitfalls
