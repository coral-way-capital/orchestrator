#!/usr/bin/env python3
"""
CWC Issue Webhook Receiver — lightweight HTTP server that receives GitHub issue events
and enqueues them to the issue queue. Runs alongside the Hermes gateway on maya.

Port: 8646 (next to Hermes gateway on 8644)
Started by: systemd service cwc-issue-webhook

Also handles:
- Epic decomposition: large issues routed to decompose queue
- Mission Control dashboard: /dashboard/ serves the SPA
- API endpoints: /api/* for queue, events, stats, decompose tree
"""

import hmac
import hashlib
import json
import re
import subprocess
import sys
import os
import time
import threading
import mimetypes
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import unquote

# Add parent to path for queue import
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(BASE_DIR))
from queue import enqueue
from events import init_db, log_event, query_events, get_stats, get_decompose_tree

DASHBOARD_DIR = BASE_DIR / "dashboard"
PROMPTS_DIR = BASE_DIR / "prompts"
REPO_MAP = {
    "coral-way-capital/audit-agent": "/home/deploy/apps/audit-agent",
    "coral-way-capital/website": "/var/www/coralwaycapital",
    "coral-way-capital/rsm-monitor": "/home/deploy/apps/eckhart",
    "coral-way-capital/eckhart": "/home/deploy/apps/eckhart",
    "coral-way-capital/zenna-crm": "/home/deploy/apps/zenna-crm",
    "coral-way-capital/inmuebles": "/home/deploy/apps/inmuebles",
    "coral-way-capital/infrastructure": "/home/deploy/apps/infrastructure",
    "coral-way-capital/tasks-cli": "/home/deploy/apps/tasks-cli",
    "coral-way-capital/agent-configs": "/home/deploy/apps/agent-configs",
    "coral-way-capital/sre": "/home/deploy/apps/sre",
    "coral-way-capital/client-status": "/home/deploy/apps/client-status",
    "coral-way-capital/diffusionZones": "/home/deploy/apps/diffusionZones",
}

# Secret is shared with GitHub webhook config
SECRET_FILE = os.path.expanduser("~/.hermes/issue-queue/webhook-secret")
QUEUE_DIR = BASE_DIR
DECOMPOSE_QUEUE_FILE = QUEUE_DIR / "decompose-queue.json"
QUEUE_FILE = QUEUE_DIR / "queue.json"
HERMES_GATEWAY = os.environ.get("HERMES_GATEWAY_URL", "http://127.0.0.1:8644")
EPIC_DECOMPOSER_ROUTE = "epic-decomposer"
MAX_DECOMPOSE_PENDING = 10

# Initialize DB on module load
init_db()


def load_secret():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE) as f:
            return f.read().strip()
    return None


def verify_signature(payload_body, signature_header):
    """Verify GitHub webhook HMAC-SHA256 signature."""
    secret = load_secret()
    if not secret:
        print("WARNING: No webhook secret configured, skipping verification")
        return True
    if not signature_header:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), payload_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def classify_issue_size(body):
    """
    Classify an issue as SMALL, MEDIUM, or LARGE based on heuristics.
    Returns (size, reason) where size is one of "small", "medium", "large".
    """
    if not body:
        return "small", "empty body"

    body_length = len(body)
    sections = body.count("\n## ")
    items = len(re.findall(r'^[\s]*[-*]\s|^\s*\d+\.\s', body, re.MULTILINE))
    has_ac = bool(re.search(
        r'##\s*(Acceptance criteria|Criterios de aceptación|Aceptación|AC)',
        body, re.IGNORECASE
    ))
    has_pilots = bool(re.search(r'(pilot|piloto|phase|etapa|milestone)', body, re.IGNORECASE))
    has_work_items = bool(re.search(
        r'##\s*(Work required|Trabajo requerido|Required work|Work items)',
        body, re.IGNORECASE
    ))

    score = 0
    if body_length > 5000:
        score += 2
    elif body_length > 2500:
        score += 1
    if sections >= 6:
        score += 2
    elif sections >= 4:
        score += 1
    if items >= 15:
        score += 2
    elif items >= 8:
        score += 1
    if has_ac and has_work_items:
        score += 1
    if has_pilots:
        score += 1

    if score >= 7:
        return "large", f"score={score} (len={body_length}, sections={sections}, items={items})"
    elif score >= 4:
        return "medium", f"score={score} (len={body_length}, sections={sections}, items={items})"
    else:
        return "small", f"score={score} (len={body_length}, sections={sections}, items={items})"


def load_decompose_queue():
    if DECOMPOSE_QUEUE_FILE.exists():
        with open(DECOMPOSE_QUEUE_FILE) as f:
            return json.load(f)
    return {"pending": [], "completed": [], "failed": []}


def save_decompose_queue(queue):
    DECOMPOSE_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if len(queue["completed"]) > 100:
        queue["completed"] = queue["completed"][-100:]
    with open(DECOMPOSE_QUEUE_FILE, "w") as f:
        json.dump(queue, f, indent=2)


def enqueue_for_decomposition(repo, issue_number, title, body, author, labels, html_url):
    """Add a large issue to the decompose queue."""
    queue = load_decompose_queue()
    item_id = f"{repo}#{issue_number}"

    all_ids = [x["id"] for x in queue["pending"]]
    if item_id in all_ids:
        print(f"DECOMPOSE SKIP: {item_id} already in decompose queue")
        return False

    if len(queue["pending"]) >= MAX_DECOMPOSE_PENDING:
        log_event("guard.triggered", item_id=item_id, repo=repo,
                  issue_number=issue_number, title=title,
                  details={"guard": "decompose_queue_full", "max": MAX_DECOMPOSE_PENDING})
        print(f"DECOMPOSE SKIP: queue full ({MAX_DECOMPOSE_PENDING})")
        return False

    item = {
        "id": item_id,
        "repo": repo,
        "issue_number": issue_number,
        "title": title,
        "body": body or "",
        "author": author,
        "labels": labels,
        "html_url": html_url,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }

    queue["pending"].append(item)
    save_decompose_queue(queue)
    log_event("decompose.enqueued", item_id=item_id, repo=repo,
              issue_number=issue_number, title=title,
              details={"author": author, "labels": labels})
    print(f"DECOMPOSE ENQUEUED: {item_id} — {title}")
    return True


def forward_to_hermes_gateway(payload_bytes):
    secret = load_secret()
    if not secret:
        print("WARN: No webhook secret, cannot forward to Hermes gateway")
        return

    sig = "sha256=" + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256
    ).hexdigest()

    url = f"{HERMES_GATEWAY}/webhooks/{EPIC_DECOMPOSER_ROUTE}"

    def _post():
        try:
            req = Request(
                url,
                data=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "issues",
                    "X-Hub-Signature-256": sig,
                },
                method="POST",
            )
            resp = urlopen(req, timeout=10)
            print(f"FORWARDED to Hermes gateway: {resp.status} {resp.read().decode()[:100]}")
        except Exception as e:
            print(f"ERROR forwarding to Hermes gateway: {e}")

    thread = threading.Thread(target=_post, daemon=True)
    thread.start()


def load_queue_json():
    """Load the main issue queue."""
    if QUEUE_FILE.exists():
        with open(QUEUE_FILE) as f:
            return json.load(f)
    return {"pending": [], "in_progress": [], "completed": [], "failed": []}


class IssueWebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = unquote(self.path.split("?")[0])
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # API dispatch route
        if path == "/api/dispatch":
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                self._json_response({"error": "Invalid JSON"}, 400)
                return
            item_id = payload.get("item_id")
            prompt_id = payload.get("prompt_id")
            if not item_id or not prompt_id:
                self._json_response({"error": "item_id and prompt_id required"}, 400)
                return
            # Look up item details from queue (search all lists for re-dispatch)
            from queue import load_queue
            queue = load_queue()
            item = None
            source_list_name = None
            for lst_name in ("pending", "in_progress", "completed", "failed"):
                for i in queue[lst_name]:
                    if i["id"] == item_id:
                        item = i
                        source_list_name = lst_name
                        break
                if item:
                    break
            if not item:
                self._json_response({"error": f"Item {item_id} not found"}, 404)
                return
            result, status = self._dispatch_agent(
                item_id, item["repo"], item["issue_number"],
                item["title"], item["body"], item["html_url"], prompt_id
            )
            self._json_response(result, status)
            return

        # GitHub webhook
        signature = self.headers.get("X-Hub-Signature-256", "")

        if not verify_signature(raw_body, signature):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        event = self.headers.get("X-GitHub-Event", "")
        action = payload.get("action", "")

        if event == "issues" and action == "opened":
            issue = payload.get("issue", {})
            repo = payload.get("repository", {}).get("full_name", "")
            labels = [l.get("name", "") for l in issue.get("labels", [])]
            issue_body = issue.get("body", "") or ""
            title = issue.get("title", "")
            issue_number = issue.get("number", 0)
            author = issue.get("user", {}).get("login", "")
            html_url = issue.get("html_url", "")
            item_id = f"{repo}#{issue_number}"

            log_event("webhook.received", item_id=item_id, repo=repo,
                      issue_number=issue_number, title=title,
                      details={"action": action, "author": author, "labels": labels},
                      source="webhook")

            # RECURSION GUARD: epic-child label
            if "epic-child" in labels:
                log_event("guard.triggered", item_id=item_id, repo=repo,
                          issue_number=issue_number, title=title,
                          details={"guard": "epic-child-label", "reason": "skipping child issue"},
                          source="webhook")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "skipped-child",
                    "reason": "epic-child label present"
                }).encode())
                return

            # RECURSION GUARD 2: title pattern
            if re.match(r"^\[Parent #\d+\]", title):
                log_event("guard.triggered", item_id=item_id, repo=repo,
                          issue_number=issue_number, title=title,
                          details={"guard": "child-title-pattern", "reason": "skipping child by title"},
                          source="webhook")
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "skipped-child",
                    "reason": "child issue by title pattern"
                }).encode())
                return

            # Classify issue size
            size, reason = classify_issue_size(issue_body)
            log_event("webhook.classified", item_id=item_id, repo=repo,
                      issue_number=issue_number, title=title,
                      details={"size": size, "reason": reason},
                      source="webhook")

            if size == "large":
                decomposed = enqueue_for_decomposition(
                    repo=repo, issue_number=issue_number, title=title,
                    body=issue_body, author=author, labels=labels, html_url=html_url,
                )
                if decomposed:
                    forward_to_hermes_gateway(raw_body)
                self.send_response(200)
                self.end_headers()
                status = "decompose-triggered" if decomposed else "decompose-skipped"
                self.wfile.write(json.dumps({"status": status, "size": size, "reason": reason}).encode())
            else:
                enqueued = enqueue(
                    repo=repo, issue_number=issue_number, title=title,
                    body=issue_body, author=author, labels=labels, html_url=html_url,
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({
                    "status": "enqueued" if enqueued else "skipped",
                    "size": size, "reason": reason
                }).encode())
        elif event == "ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong")
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ignored")

    def do_GET(self):
        # Normalize path (strip query string for routing)
        path = unquote(self.path.split("?")[0])

        # API routes
        if path == "/api/queue":
            self._json_response(load_queue_json())
        elif path == "/api/queue-enriched":
            self._json_response(self._enriched_queue())
        elif path == "/api/decompose-queue":
            self._json_response(load_decompose_queue())
        elif path == "/api/stats":
            self._json_response(get_stats())
        elif path == "/api/events":
            params = self._parse_qs()
            events = query_events(
                event_type=params.get("type"),
                repo=params.get("repo"),
                item_id=params.get("item_id"),
                since=params.get("since"),
                limit=int(params.get("limit", 100)),
                offset=int(params.get("offset", 0)),
            )
            self._json_response(events)
        elif path == "/api/decompose-tree":
            self._json_response(get_decompose_tree())
        elif path == "/api/repos":
            q = load_queue_json()
            repos = sorted(set(x["repo"] for x in q["pending"] + q["in_progress"] + q["completed"] + q["failed"]))
            self._json_response(repos)
        elif path == "/api/issue":
            params = self._parse_qs()
            repo = params.get("repo")
            number = params.get("number")
            if not repo or not number:
                self._json_response({"error": "repo and number required"}, 400)
                return
            try:
                result = subprocess.run(
                    ["gh", "api", f"repos/{repo}/issues/{number}"],
                    capture_output=True, text=True, timeout=15
                )
                if result.returncode != 0:
                    self._json_response({"error": result.stderr[:200]}, 502)
                    return
                issue = json.loads(result.stdout)
                # Also fetch comments
                comments_result = subprocess.run(
                    ["gh", "api", f"repos/{repo}/issues/{number}/comments", "--jq", ".[] | {user: .user.login, body, created_at}"],
                    capture_output=True, text=True, timeout=15
                )
                comments = []
                if comments_result.returncode == 0 and comments_result.stdout.strip():
                    for line in comments_result.stdout.strip().split("\n"):
                        try:
                            comments.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                issue["_comments"] = comments
                self._json_response(issue)
            except Exception as e:
                self._json_response({"error": str(e)[:200]}, 500)
        elif path == "/api/prompts":
            prompts = self._load_prompts()
            self._json_response(prompts)
        elif path == "/api/agent-status":
            params = self._parse_qs()
            item_id = params.get("item_id")
            if not item_id:
                self._json_response({"error": "item_id required"}, 400)
                return
            # Find the item across all queue lists
            q = load_queue_json()
            item = None
            source_list_name = None
            for lst in ("pending", "in_progress", "completed", "failed"):
                for i in q.get(lst, []):
                    if i["id"] == item_id:
                        item = i
                        source_list_name = lst
                        break
                if item:
                    break
            if not item:
                self._json_response({"error": "item not found"}, 404)
                return
            result = {
                "item_id": item_id,
                "status": "unknown",
                "pid": item.get("agent_pid"),
                "log_file": item.get("agent_log"),
                "prompt": item.get("agent_prompt"),
                "started_at": item.get("agent_started_at"),
                "log_tail": None,
                "linked_prs": [],
            }
            # Check if process is alive
            pid = item.get("agent_pid")
            if pid:
                try:
                    check = subprocess.run(["kill", "-0", str(pid)], capture_output=True, timeout=5)
                    result["status"] = "running" if check.returncode == 0 else "exited"
                except Exception:
                    result["status"] = "unknown"
            elif item.get("started_at"):
                result["status"] = "exited"

            # Auto-complete: if agent exited and item still in_progress, move to completed/failed
            if result["status"] == "exited" and source_list_name == "in_progress":
                from queue import load_queue as _lq, save_queue as _sq
                q = _lq()
                for i in q["in_progress"]:
                    if i["id"] == item_id:
                        i["completed_at"] = datetime.now(timezone.utc).isoformat()
                        # Check log for errors
                        log_ok = True
                        if log_content and ("Traceback" in log_content or "KeyboardInterrupt" in log_content):
                            log_ok = False
                        q["in_progress"].remove(i)
                        if log_ok:
                            q["completed"].append(i)
                            log_event("issue.completed", item_id=item_id, repo=item.get("repo"),
                                      issue_number=item.get("issue_number"),
                                      details={"auto": True, "source": "agent-status-check"})
                        else:
                            i["error"] = "Agent exited with error"
                            q["failed"].append(i)
                            log_event("issue.failed", item_id=item_id, repo=item.get("repo"),
                                      issue_number=item.get("issue_number"),
                                      details={"auto": True, "source": "agent-status-check"})
                        _sq(q)
                        break
            # Read log tail
            log_file = item.get("agent_log")
            log_content = None
            if log_file and Path(log_file).exists():
                try:
                    tail_bytes = 15000
                    with open(log_file, "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        if size > tail_bytes:
                            f.seek(size - tail_bytes)
                            f.readline()
                        else:
                            f.seek(0)
                        log_content = f.read().decode("utf-8", errors="replace")
                except Exception as e:
                    log_content = f"Error reading log: {e}"

            # If log file is empty, try hermes agent.log for live activity
            if not log_content or log_content.strip() == "":
                agent_log = Path.home() / ".hermes" / "logs" / "agent.log"
                if agent_log.exists():
                    try:
                        # Get last 200 lines from agent.log (live tool activity)
                        r = subprocess.run(
                            ["tail", "-200", str(agent_log)],
                            capture_output=True, text=True, timeout=5
                        )
                        if r.returncode == 0 and r.stdout.strip():
                            # Filter to recent activity (tool calls, errors)
                            lines = r.stdout.strip().split("\n")
                            filtered = []
                            for line in lines:
                                if any(kw in line.lower() for kw in ["tool", "error", "warning", "subagent", "delegat"]):
                                    # Extract just the message part
                                    parts = line.split(" INFO ", 1)
                                    if len(parts) > 1:
                                        filtered.append(parts[1])
                                    elif len(parts) == 1:
                                        # Try WARN/ERROR
                                        for lvl in (" WARN ", " ERROR "):
                                            p = line.split(lvl, 1)
                                            if len(p) > 1:
                                                filtered.append(p[1])
                                                break
                            if filtered:
                                log_content = "[Live agent.log — tool activity]\n" + "\n".join(filtered[-50:])
                    except Exception:
                        pass

            result["log_tail"] = log_content
            # Check for linked PRs on the GitHub issue
            repo = item.get("repo")
            issue_number = item.get("issue_number")
            if repo and issue_number:
                try:
                    from queue import check_linked_prs
                    prs = check_linked_prs(repo, issue_number)
                    result["linked_prs"] = prs or []
                except Exception:
                    pass
            self._json_response(result)
        elif path == "/api/sync":
            repo_filter = self._parse_qs().get("repo")
            from queue import sync_github_issues
            results = sync_github_issues(repo_filter)
            self._json_response(results)
        elif path == "/api/health":
            dq = load_decompose_queue()
            q = load_queue_json()
            self._json_response({
                "status": "ok",
                "service": "cwc-mission-control",
                "queue": {
                    "pending": len(q["pending"]),
                    "in_progress": len(q["in_progress"]),
                    "completed": len(q["completed"]),
                    "failed": len(q["failed"]),
                },
                "decompose": {
                    "pending": len(dq["pending"]),
                    "completed": len(dq["completed"]),
                    "failed": len(dq["failed"]),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        elif path == "/health":
            dq = load_decompose_queue()
            self._json_response({
                "status": "ok",
                "service": "cwc-issue-webhook",
                "decompose_queue": {
                    "pending": len(dq["pending"]),
                    "completed": len(dq["completed"]),
                    "failed": len(dq["failed"]),
                }
            })
        elif path == "/decompose-status":
            self._json_response(load_decompose_queue())
        # Dashboard routes
        elif path in ("/", "/dashboard", "/dashboard/"):
            self._serve_file(DASHBOARD_DIR / "index.html", "text/html")
        elif path.startswith("/dashboard/"):
            rel = path[len("/dashboard/"):]
            self._serve_file(DASHBOARD_DIR / rel)
        else:
            self.send_response(404)
            self.end_headers()

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2, default=str).encode())

    def _enriched_queue(self):
        """Return queue with cycle times and event counts per item."""
        queue = load_queue_json()
        cycle_map = {}
        event_count_map = {}
        try:
            with get_db() as db:
                # Cycle times: enqueued → completed
                rows = db.execute("""
                    SELECT e1.item_id,
                           e1.timestamp as started,
                           e2.timestamp as completed
                    FROM events e1
                    JOIN events e2 ON e1.item_id = e2.item_id
                    WHERE e1.event_type = 'issue.enqueued'
                      AND e2.event_type = 'issue.completed'
                """).fetchall()
                for r in rows:
                    try:
                        from datetime import datetime as _dt
                        t1 = _dt.fromisoformat(r["started"].replace("Z", "+00:00"))
                        t2 = _dt.fromisoformat(r["completed"].replace("Z", "+00:00"))
                        mins = (t2 - t1).total_seconds() / 60
                        if mins >= 0:
                            cycle_map[r["item_id"]] = round(mins, 1)
                    except Exception:
                        pass

                # Event counts per item
                rows = db.execute("""
                    SELECT item_id, COUNT(*) as cnt
                    FROM events
                    WHERE item_id IS NOT NULL
                    GROUP BY item_id
                """).fetchall()
                for r in rows:
                    event_count_map[r["item_id"]] = r["cnt"]
        except Exception:
            pass

        for status in ("pending", "in_progress", "completed", "failed"):
            for item in queue.get(status, []):
                item["cycle_minutes"] = cycle_map.get(item["id"])
                item["event_count"] = event_count_map.get(item["id"], 0)
        return queue

    def _parse_qs(self):
        """Parse query string into dict."""
        from urllib.parse import unquote_plus
        if "?" not in self.path:
            return {}
        qs = self.path.split("?", 1)[1]
        params = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = unquote_plus(v)
        return params

    def _serve_file(self, filepath, content_type=None):
        """Serve a static file."""
        if not filepath.exists():
            self.send_response(404)
            self.end_headers()
            return
        if content_type is None:
            ct, _ = mimetypes.guess_type(str(filepath))
            content_type = ct or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(filepath, "rb") as f:
            self.wfile.write(f.read())

    def _load_prompts(self):
        """Load prompt templates from the prompts/ directory."""
        prompts = []
        if not PROMPTS_DIR.exists():
            return prompts
        for f in sorted(PROMPTS_DIR.iterdir()):
            if f.suffix not in ('.md', '.txt'):
                continue
            content = f.read_text()
            # Parse YAML frontmatter
            name = f.stem
            description = ""
            body = content
            if content.startswith('---'):
                end = content.find('---', 3)
                if end > 0:
                    import yaml
                    try:
                        meta = yaml.safe_load(content[3:end])
                        name = meta.get('name', name)
                        description = meta.get('description', '')
                    except Exception:
                        pass
                    body = content[end + 3:].strip()
            prompts.append({
                "id": f.stem,
                "name": name,
                "description": description,
                "body": body,
            })
        return prompts

    def _render_prompt(self, template_body, variables):
        """Render a prompt template with {{variable}} substitution."""
        result = template_body
        for key, value in variables.items():
            result = result.replace('{{' + key + '}}', str(value))
        return result

    def _dispatch_agent(self, item_id, repo, issue_number, title, body, html_url, prompt_id):
        """Spawn a pi agent to work on an issue. Returns agent info."""
        from queue import load_queue, save_queue, next_pending

        # Load prompt template
        prompts = self._load_prompts()
        prompt = next((p for p in prompts if p["id"] == prompt_id), None)
        if not prompt:
            return {"error": f"Prompt '{prompt_id}' not found"}, 400

        local_path = REPO_MAP.get(repo, f"/home/deploy/apps/{repo.split('/')[-1]}")

        # Render the prompt
        rendered = self._render_prompt(prompt["body"], {
            "title": title,
            "repo": repo,
            "local_path": local_path,
            "html_url": html_url,
            "body": body or "",
            "issue_number": str(issue_number),
        })

        # Claim the item (move to in_progress)
        queue = load_queue()
        item = None
        source_list = None
        for lst_name in ("pending", "in_progress", "completed", "failed"):
            for i in queue[lst_name]:
                if i["id"] == item_id:
                    item = i
                    source_list = lst_name
                    break
            if item:
                break
        if not item:
            return {"error": f"Item {item_id} not found"}, 404

        # If in_progress, check if agent is still alive
        if source_list == "in_progress" and item.get("agent_pid"):
            try:
                check = subprocess.run(["kill", "-0", str(item["agent_pid"])], capture_output=True, timeout=5)
                if check.returncode == 0:
                    return {"error": "Agent still running", "item": item}, 409
            except Exception:
                pass

        # Reset agent fields and move to in_progress
        for k in ("started_at", "agent_pid", "agent_log", "agent_prompt", "agent_started_at", "completed_at", "error", "closed_via"):
            item[k] = None
        if source_list != "pending":
            queue[source_list].remove(item)
            queue["in_progress"].append(item)
        else:
            queue["pending"].remove(item)
            queue["in_progress"].append(item)
        save_queue(queue)
        item["started_at"] = datetime.now(timezone.utc).isoformat()
        log_event("issue.claimed", item_id=item_id, repo=repo,
                  issue_number=issue_number, title=title,
                  details={"source": "manual_dispatch", "prompt": prompt_id})

        # Write prompt to temp file so hermes can read it
        import tempfile
        safe_id = item_id.replace("/", "_").replace("#", "-")
        prompt_file = Path(tempfile.mktemp(suffix=".md", prefix=f"dispatch-{safe_id}-"))
        prompt_file.write_text(rendered)

        # Build the hermes command
        # Use python -u (unbuffered) directly for live log output
        hermes_python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
        hermes_main = Path.home() / ".hermes" / "hermes-agent" / "hermes_cli" / "main.py"
        log_file = Path(tempfile.mktemp(suffix=".log", prefix=f"agent-{safe_id}-"))

        cmd = (
            f"cd {local_path} && "
            f"PYTHONUNBUFFERED=1 GITHUB_TOKEN={os.environ.get('GITHUB_TOKEN', '')} "
            f"{hermes_python} -u {hermes_main} chat -q \"$(cat {prompt_file})\" -Q --yolo"
            f" > {log_file} 2>&1 &"
            f" echo $!"
        )

        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            pid = result.stdout.strip()
            if not pid:
                # Failed to start, move back to pending
                queue = load_queue()
                for i in queue["in_progress"]:
                    if i["id"] == item_id:
                        i["started_at"] = None
                        queue["in_progress"].remove(i)
                        queue["pending"].insert(0, i)
                        save_queue(queue)
                        break
                return {"error": "Failed to start agent", "stderr": result.stderr[:200]}, 500

            # Track the agent
            queue = load_queue()
            for i in queue["in_progress"]:
                if i["id"] == item_id:
                    i["agent_pid"] = pid
                    i["agent_log"] = str(log_file)
                    i["agent_prompt"] = prompt_id
                    i["agent_started_at"] = datetime.now(timezone.utc).isoformat()
                    save_queue(queue)
                    break

            log_event("issue.dispatched", item_id=item_id, repo=repo,
                      issue_number=issue_number, title=title,
                      details={"pid": pid, "prompt": prompt_id, "log": str(log_file)})

            return {
                "ok": True,
                "item_id": item_id,
                "pid": pid,
                "log_file": str(log_file),
                "prompt": prompt_id,
                "local_path": local_path,
            }, 200

        except Exception as e:
            # Move back to pending on error
            queue = load_queue()
            for i in queue["in_progress"]:
                if i["id"] == item_id:
                    i["started_at"] = None
                    queue["in_progress"].remove(i)
                    queue["pending"].insert(0, i)
                    save_queue(queue)
                    break
            return {"error": str(e)[:200]}, 500

    def do_PATCH(self):
        """Handle prioritization: PATCH /api/queue/prioritize/<id> or PATCH /api/queue/move-down/<id>"""
        path = unquote(self.path.split("?")[0])
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            payload = json.loads(raw_body) if raw_body else {}
        except Exception:
            payload = {}

        if path.startswith("/api/queue/prioritize/"):
            item_id = path[len("/api/queue/prioritize/"):]
            from queue import prioritize_top
            ok = prioritize_top(item_id)
            self._json_response({"ok": ok, "item_id": item_id, "action": "prioritize_top"})
        elif path.startswith("/api/queue/move-down/"):
            item_id = path[len("/api/queue/move-down/"):]
            from queue import move_down
            ok = move_down(item_id)
            self._json_response({"ok": ok, "item_id": item_id, "action": "move_down"})
        elif path.startswith("/api/queue/move-to-bottom/"):
            item_id = path[len("/api/queue/move-to-bottom/"):]
            from queue import move_to_bottom
            ok = move_to_bottom(item_id)
            self._json_response({"ok": ok, "item_id": item_id, "action": "move_to_bottom"})
        elif path.startswith("/api/queue/retry/"):
            item_id = path[len("/api/queue/retry/"):]
            from queue import retry
            ok = retry(item_id)
            self._json_response({"ok": ok, "item_id": item_id, "action": "retry"})
        elif path.startswith("/api/queue/remove/"):
            item_id = path[len("/api/queue/remove/"):]
            from queue import reset as queue_reset
            ok = queue_reset(item_id)
            self._json_response({"ok": ok, "item_id": item_id, "action": "remove"})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[mission-control] {args[0]}")


def main():
    port = int(os.environ.get("PORT", 8646))
    server = HTTPServer(("100.102.201.26", port), IssueWebhookHandler)
    print(f"CWC Mission Control listening on 127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
