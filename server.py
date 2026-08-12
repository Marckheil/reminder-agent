#!/usr/bin/env python3
# The Snitch — MULTI-USER accountability agent on Maritime.
#
# One agent, many users. Each user is keyed by their email address.
# Flow:
#   1. Someone signs up (via the Vercel page -> POST /signup with
#      email, snitch contact, timezone). The agent registers them and
#      immediately emails them: "What's your goal for today?"
#   2. The user replies to that email with goals + deadlines, e.g.:
#          gym by 2pm
#          essay by 6pm
#      (Their snitch contact + timezone came from signup, so they don't
#       re-enter them.)
#   3. On each wake, the agent:
#        - reads NEW inbound emails, matches each to a user by sender,
#          and either sets their goals or clears a "done" item
#        - checks every user's goals; any past deadline + not done ->
#          emails THAT user's snitch contact to roast them.
#
# Storage (all on the agent's own disk):
#   users.json  -> { email: {snitch, tz_offset, goals:[...] } }
#   seen.json   -> id of the last inbound email processed
#
# Endpoints:
#   POST /signup   { "email":..., "snitch":..., "tz_offset":-5 }  -> registers + emails them
#   GET  /run      -> process new mail + fire due snitches
#   GET  /health   -> liveness + user count
#   GET  /         -> tiny status page

import os, re, json, pathlib
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from inkbox import Inkbox

PORT = int(os.environ.get("PORT", "8080"))
API_KEY = os.environ["INKBOX_API_KEY"]
HANDLE = os.environ.get("AGENT_HANDLE", "reminder")

DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", "."))
USERS_FILE = DATA_DIR / "users.json"
SEEN_FILE = DATA_DIR / "seen.json"


# ---------- persistence ----------
def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2))


def load_users():
    return load(USERS_FILE, {})


def save_users(u):
    save(USERS_FILE, u)


# ---------- deadline parsing (timezone-aware) ----------
def parse_deadline(text, now_utc, tz_offset):
    """'gym by 2pm' -> ('gym', deadline_utc_iso). tz_offset is the user's hours from UTC."""
    m = re.search(r"\bby\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.IGNORECASE)
    if not m:
        return text.strip(), None
    hour = int(m.group(1)); minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12: hour += 12
    if ampm == "am" and hour == 12: hour = 0
    # the user means <hour> in THEIR local time. Convert to UTC.
    user_now = now_utc + timedelta(hours=tz_offset)
    local_deadline = user_now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    utc_deadline = local_deadline - timedelta(hours=tz_offset)
    clean = text[:m.start()].strip() or text.strip()
    return clean, utc_deadline.isoformat()


def parse_goals(body, now_utc, tz_offset):
    goals = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        # ignore quoted reply lines / our own prompt text
        if line.startswith(">") or line.lower().startswith("what's your goal"):
            continue
        text, deadline = parse_deadline(line, now_utc, tz_offset)
        goals.append({"text": text, "deadline": deadline, "status": "pending"})
    return goals


def parse_done_reply(body):
    first = (body.strip().splitlines() or [""])[0].strip().lower()
    m = re.match(r"done\b\s*(.*)", first)
    return m.group(1).strip() if m else None


# ---------- email helpers ----------
def send_goal_prompt(identity, email):
    identity.send_email(
        to=[email],
        subject="What's your goal for today?",
        body_text=(
            "Morning. What are you getting done today?\n\n"
            "Reply with one goal per line and a deadline, like:\n"
            "  gym by 2pm\n"
            "  finish essay by 6pm\n\n"
            "If you miss a deadline, I email your accountability partner to roast you.\n"
            "Reply 'done gym' when you finish something so it's safe.\n\n"
            "- The Snitch"
        ),
    )


def send_snitch(identity, user_email, snitch_email, goal_text):
    identity.send_email(
        to=[snitch_email],
        subject=f"{user_email} failed a goal. Roast them.",
        body_text=(
            f"Hey - I'm {user_email}'s accountability agent.\n\n"
            f"They promised to \"{goal_text}\" and blew the deadline.\n\n"
            f"Your job: roast them. Mercilessly. They signed up for this.\n\n"
            f"- The Snitch"
        ),
    )


# ---------- core cycle ----------
def run_cycle():
    now = datetime.now(timezone.utc)
    users = load_users()
    seen = load(SEEN_FILE, {}).get("id")
    summary = {"processed_email": None, "cleared": 0, "snitched": [], "users": len(users)}

    with Inkbox(api_key=API_KEY) as inkbox:
        identity = inkbox.get_identity(HANDLE)

        # 1. process the newest inbound email, matched to a user by sender
        newest = None
        for msg in identity.iter_emails(direction="inbound"):
            newest = msg
            break

        if newest is not None and str(newest.id) != seen:
            save(SEEN_FILE, {"id": str(newest.id)})
            sender = (newest.from_address or "").strip().lower()
            summary["processed_email"] = sender
            if sender in users:
                detail = identity.get_message(str(newest.id))
                body = detail.body_text or ""
                u = users[sender]
                done_kw = parse_done_reply(body)
                if done_kw is not None:
                    for g in u["goals"]:
                        if g["status"] == "pending" and (done_kw == "" or done_kw in g["text"].lower()):
                            g["status"] = "done"; summary["cleared"] += 1
                else:
                    goals = parse_goals(body, now, u.get("tz_offset", 0))
                    if goals:
                        u["goals"] = goals
                save_users(users)

        # 2. check every user's goals, snitch on misses
        for email, u in users.items():
            for g in u["goals"]:
                if g["status"] != "pending" or not g["deadline"]:
                    continue
                if now >= datetime.fromisoformat(g["deadline"]):
                    if u.get("snitch"):
                        send_snitch(identity, email, u["snitch"], g["text"])
                    g["status"] = "snitched"
                    summary["snitched"].append({"user": email, "goal": g["text"]})
        save_users(users)

    return summary


def register_user(email, snitch, tz_offset):
    email = email.strip().lower()
    users = load_users()
    users[email] = {"snitch": (snitch or "").strip(),
                    "tz_offset": tz_offset,
                    "goals": []}
    save_users(users)
    # immediately email them the goal prompt
    with Inkbox(api_key=API_KEY) as inkbox:
        identity = inkbox.get_identity(HANDLE)
        send_goal_prompt(identity, email)
    return {"ok": True, "email": email}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")  # allow the Vercel page to call us
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path.startswith("/signup"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                email = data.get("email")
                if not email:
                    return self._json(400, {"ok": False, "error": "email required"})
                res = register_user(email, data.get("snitch", ""),
                                    int(data.get("tz_offset", 0)))
                self._json(200, res)
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:400]})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_GET(self):
        if self.path.startswith("/run"):
            try:
                self._json(200, {"ok": True, **run_cycle()})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)[:400]})
        elif self.path.startswith("/health"):
            self._json(200, {"status": "ok", "users": len(load_users())})
        else:
            n = len(load_users())
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                (f"<h1>The Snitch</h1><p>{n} users being held accountable.</p>").encode())

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"snitch (multi-user) listening on :{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
