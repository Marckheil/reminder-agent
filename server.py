#!/usr/bin/env python3
# Reminder Agent — runs on Maritime as a sleep/wake web agent.
#
# Design: the agent sleeps by default. Something (a cron trigger, or a manual
# poke) sends an HTTP request to /run, which wakes the agent. On each wake it
# does ONE cycle:
#   1. read any new task emails the user sent to its Inkbox address
#   2. store the newest task list
#   3. email the user a reminder of what's still on the list
# Then it goes back to sleep until the next poke. No always-on loop.
#
# Endpoints:
#   GET /run     -> do one cycle, return JSON summary
#   GET /health  -> liveness + current stored tasks
#   GET /        -> tiny status page

import os, json, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from inkbox import Inkbox

PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ["INKBOX_API_KEY"]        # set via `maritime env set`
HANDLE = os.environ.get("AGENT_HANDLE", "reminder")
YOUR_EMAIL = os.environ["OWNER_EMAIL"]        # who to remind

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "."))
TASKS_FILE = DATA_DIR / "tasks.json"
SEEN_FILE = DATA_DIR / "last_seen.json"       # remember last email we processed


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def run_cycle():
    """One wake = one cycle. Returns a summary dict."""
    summary = {"read_new_tasks": False, "sent_reminder": False, "tasks": []}

    with Inkbox(api_key=API_KEY) as inkbox:
        identity = inkbox.get_identity(HANDLE)

        # 1. look at the newest inbound email; if it's new, treat it as the task list
        last_seen = load_json(SEEN_FILE, {}).get("id")
        newest = None
        for msg in identity.iter_emails(direction="inbound"):
            newest = msg
            break

        if newest is not None and str(newest.id) != last_seen:
            detail = identity.get_message(str(newest.id))
            body = detail.body_text or ""
            tasks = [ln.strip() for ln in body.splitlines() if ln.strip()]
            if tasks:
                TASKS_FILE.write_text(json.dumps(tasks, indent=2))
                SEEN_FILE.write_text(json.dumps({"id": str(newest.id)}))
                summary["read_new_tasks"] = True

        # 2. load whatever the current task list is
        tasks = load_json(TASKS_FILE, [])
        summary["tasks"] = tasks

        # 3. if there are tasks, send a reminder
        if tasks:
            lines = "\n".join(f"- {t}" for t in tasks)
            body = (
                f"Hey — here's what you said you'd get done:\n\n{lines}\n\n"
                f"You've got this. (Reply with a new list anytime to update me.)"
            )
            identity.send_email(
                to=[YOUR_EMAIL],
                subject="Your reminder",
                body_text=body,
            )
            summary["sent_reminder"] = True

    return summary


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        if self.path.startswith("/run"):
            try:
                self._json(200, {"ok": True, **run_cycle()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:300]})
        elif self.path.startswith("/health"):
            self._json(200, {"status": "ok", "tasks": load_json(TASKS_FILE, [])})
        else:
            tasks = load_json(TASKS_FILE, [])
            html = (
                "<h1>Reminder Agent</h1>"
                f"<p>Stored tasks: {len(tasks)}</p>"
                "<p>Hit <code>/run</code> to do one cycle.</p>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())

    def log_message(self, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"reminder agent listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
