GitHub push event received. Repo: {repository.full_name}, Ref: {ref}, Pusher: {pusher.name}, Commit: {after}.

If repository.full_name is 'coral-way-capital/website' AND ref is 'refs/heads/main':
  1. Run: cd /home/deploy/apps/website && git checkout main && git pull origin main
  2. Run: cd /var/www/coralwaycapital && git checkout main && git pull origin main
  3. Report which files changed (git diff --stat HEAD@{1} HEAD) and confirm the site is live.
  4. Deliver result to discord:1468240937833467971 with a brief summary.

If this is NOT the website repo or NOT the main branch, respond with exactly: SKIP — not a website main push. Do nothing else.