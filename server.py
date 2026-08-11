#!/usr/bin/env python3
# The Snitch — an accountability agent that runs on Maritime.
#
# You email it your goals WITH deadlines, plus a "snitch" contact.
# It sleeps. On each wake (cron/poke) it checks every goal:
#   - past its deadline AND you haven't marked it done  -> it EMAILS YOUR SNITCH
#     ("Marckheil said he'd X and didn't. Roast him.") and marks it snitched.
# You clear goals by replying "done <keyword>" (or just "done" for all).
#
# Input email format (to the agent's Inkbox address):
#     snitch: friend@example.com
#     gym by 2pm
#     essay by 6pm
#     call mom by 9pm
#
# Reply to clear:  "done gym"   or   "done"   (clears everything)

import os, re, json, pathlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from inkbox import Inkbox

PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ["INKBOX_API_KEY"]
HANDLE = os.environ.get("AGENT_HANDLE", "reminder")
OWNER_NAME = os.environ.get("OWNER_NAME", "Your friend")
OWNER_EMAIL = os.environ["OWNER_EMAIL"]

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "."))
STATE_FILE = DATA_DIR / "state.json"
SEEN_FILE = DATA_DIR / "seen.json"


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2))


def parse_deadline(text, now):
    m = re.search(r"\bby\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.IGNORECASE)
    if not m:
        return text.strip(), None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    deadline = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    clean = text[:m.start()].strip() or text.strip()
    return clean, deadline.isoformat()


def parse_task_email(body, now):
    snitch = None
    goals = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"snitch:\s*(\S+@\S+)", line, re.IGNORECASE)
        if m:
            snitch = m.group(1)
            continue
        text, deadline = parse_deadline(line, now)
        goals.append({"text": text, "deadline": deadline, "status": "pending"})
    return snitch, goals


def parse_done_reply(body):
    first = (body.strip().splitlines() or [""])[0].strip().lower()
    m = re.match(r"done\b\s*(.*)", first)
    if m:
        return m.group(1).strip()
    return None


def run_cycle():
    now = datetime.now(timezone.utc)
    state = load(STATE_FILE, {"snitch": None, "goals": []})
    seen = load(SEEN_FILE, {}).get("id")
    summary = {"new_email": False, "cleared": 0, "snitched": [], "goals": []}

    with Inkbox(api_key=API_KEY) as inkbox:
        identity = inkbox.get_identity(HANDLE)

        newest = None
        for msg in identity.iter_emails(direction="inbound"):
            newest = msg
            break

        if newest is not None and str(newest.id) != seen:
            detail = identity.get_message(str(newest.id))
            body = detail.body_text or ""
            save(SEEN_FILE, {"id": str(newest.id)})
            summary["new_email"] = True

            done_kw = parse_done_reply(body)
            if done_kw is not None:
                for g in state["goals"]:
                    if g["status"] == "pending" and (done_kw == "" or done_kw in g["text"].lower()):
                        g["status"] = "done"
                        summary["cleared"] += 1
            else:
                snitch, goals = parse_task_email(body, now)
                if goals:
                    state = {"snitch": snitch, "goals": goals}

        for g in state["goals"]:
            if g["status"] != "pending" or not g["deadline"]:
                continue
            if now >= datetime.fromisoformat(g["deadline"]):
                if state.get("snitch"):
                    body = (
                        f"Hey - I'm {OWNER_NAME}'s accountability agent.\n\n"
                        f"{OWNER_NAME} promised to \"{g['text']}\" by the deadline and didn't do it.\n\n"
                        f"Your job: roast them. Mercilessly. They signed up for this.\n\n"
                        f"- The Snitch"
                    )
                    identity.send_email(
                        to=[state["snitch"]],
                        subject=f"{OWNER_NAME} failed a goal. Roast them.",
                        body_text=body,
                    )
                g["status"] = "snitched"
                summary["snitched"].append(g["text"])

        save(STATE_FILE, state)
        summary["goals"] = state["goals"]

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
                self._json(500, {"ok": False, "error": str(e)[:400]})
        elif self.path.startswith("/health"):
            self._json(200, {"status": "ok", "state": load(STATE_FILE, {})})
        else:
            st = load(STATE_FILE, {"goals": []})
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                (f"<h1>The Snitch</h1><p>{len(st.get('goals', []))} goals tracked. "
                 f"Miss a deadline and your friend hears about it.</p>").encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"snitch agent listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
