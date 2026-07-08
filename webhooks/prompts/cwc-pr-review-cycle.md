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

STEP 1 - GATHER UNRESOLVED FEEDBACK
Write query to /tmp/threads_query.json:
{
  "query": "query($owner: String!, $repo: String!, $pr: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $pr) { reviewThreads(first: 50) { nodes { id isResolved isOutdated comments(first: 10) { nodes { id databaseId author { login } body path line } } } } } }",
  "variables": {"owner": "{repository.owner.login}", "repo": "{repository.name}", "pr": {pull_request.number}}
}
Run: gh api graphql --input /tmp/threads_query.json

Collect UNRESOLVED + NON-OUTDATED threads from the human reviewer only. Deduplicate by file:line.

STEP 2 - CHECK OUT AND FIX
a) cd /home/deploy/apps/{repository.name}
b) git stash 2>/dev/null; git checkout main
c) git branch -D pr-{pull_request.number} 2>/dev/null; true
d) git fetch origin pull/{pull_request.number}/head:pr-{pull_request.number}
e) git checkout -f pr-{pull_request.number}
f) For each unresolved actionable thread:
   1. Read the file at the referenced path and line
   2. Understand the feedback
   3. Implement the fix
   4. Resolve the thread via GraphQL:
      Write to /tmp/resolve_thread.json:
      {"query": "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) { thread { isResolved } } }", "variables": {"threadId": "<THREAD_ID>"}}
      Run: gh api graphql --input /tmp/resolve_thread.json
   5. Verify resolution succeeded

STEP 3 - VERIFY AND PUSH
a) bunx tsc --noEmit (if TypeScript files changed)
b) Run targeted tests on affected files only (use ././ prefix for file-path mode in bun test)
c) git add -A
d) git commit -m "fix: address human review feedback (cycle N/5)"
e) git push origin HEAD:refs/heads/{pull_request.head.ref}

STEP 4 - RE-REVIEW
a) Wait 10 seconds for GitHub to process the push
b) Get new HEAD SHA: git rev-parse HEAD
c) Get updated diff: git diff origin/{pull_request.base.ref}...HEAD
d) Re-review with full rigor (correctness, security, quality, testing, performance)
e) Submit review via Reviews API:
   Write review JSON to /tmp/review.json with new HEAD SHA
   Run: gh api repos/{repository.full_name}/pulls/{pull_request.number}/reviews --input /tmp/review.json
   event: "COMMENT" (bot is PR author)
   If CLEAN: body = "Re-review cycle N/5: APPROVED"
   If issues remain: body = "Re-review cycle N/5: Y issues remain" with inline comments
f) If Reviews API drops inline comments, post individually via:
   POST repos/{repository.full_name}/pulls/{pull_request.number}/comments

STEP 5 - CLEANUP
a) git checkout main
b) git branch -D pr-{pull_request.number}
c) Return: "Cycle N/5: fixed X issues, resolved Y threads | APPROVED" or "Cycle N/5: Y issues remain"

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
- If the repo directory /home/deploy/apps/{repository.name} does not exist, clone it first: git clone git@github.com:{repository.full_name}.git /home/deploy/apps/{repository.name}
- Use the github-code-review skill for the review methodology and pitfalls