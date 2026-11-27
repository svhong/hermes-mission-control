#!/usr/bin/env python3
"""hermes-mission-control hub.

Receives reports from collectors and serves the dashboard. Standard library
only; data is kept in memory (the latest report from each source).

Settings (see /etc/hermes-mission-control/hub.env):
  HMC_BIND                 address(es) to listen on, comma-separated
  HMC_PORT                 port (default 8788)
  HMC_TOKEN                shared secret collectors must send
  HMC_VIEW_PASSWORD_HASH   if set, viewers must log in
                           (create with: python3 hub/hub.py --hash-password)
  HMC_SESSION_HOURS        how long a login lasts (default 168 = 7 days)
  HMC_GARAGE_URL           optional link to your garage-monitor dashboard
  STATE_DIRECTORY          where to keep the daily token ledger (set by systemd);
                           without it the ledger lives in memory only
"""
import getpass
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("HMC_BIND", "127.0.0.1")
PORT = int(os.environ.get("HMC_PORT", "8788"))
TOKEN = os.environ.get("HMC_TOKEN", "")
PASSWORD_HASH = os.environ.get("HMC_VIEW_PASSWORD_HASH", "").strip()
SESSION_S = float(os.environ.get("HMC_SESSION_HOURS", "168")) * 3600
GARAGE_URL = os.environ.get("HMC_GARAGE_URL", "").strip()

MAX_BODY = 4 * 1024 * 1024   # largest report accepted
FORGET_AFTER_S = 7 * 86400   # drop sources silent for a week
PBKDF2_ROUNDS = 300_000
MAX_FAILURES = 5
LOCKOUT_S = 60

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_TYPES = {".html", ".js", ".css", ".svg", ".png", ".ico", ".json"}
TAB_ID = re.compile(r"^[a-z0-9_-]{1,32}$")
SOURCE_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")

LEDGER_DAYS = 16
STATE_DIR = os.environ.get("STATE_DIRECTORY", "").split(":")[0]


def ui_version():
    """Changes whenever the dashboard files change, so open pages reload themselves."""
    h = hashlib.sha256()
    for root, _, files in sorted(os.walk(STATIC_DIR)):
        for name in sorted(files):
            st = os.stat(os.path.join(root, name))
            h.update(("%s:%d:%d;" % (os.path.join(root, name), st.st_size, st.st_mtime_ns)).encode())
    return h.hexdigest()[:12]


UI_VERSION = ui_version()


class Ledger:
    """Tokens used per day, per source.

    Hermes keeps one running total per session, and a chat can run for days,
    so "tokens today" can't be read from the database. The ledger remembers
    each session's total at the start of the day (or when first seen) and
    counts the growth. Saved to STATE_DIRECTORY so restarts don't lose it.
    """

    def __init__(self, path=None):
        self.path = path
        self.days = {}  # source -> day -> {"base": {sid: t}, "cur": {sid: t}, "since": ts|None}
        self.dirty = False
        self.saved_at = 0.0
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    self.days = json.load(f)
            except (OSError, ValueError):
                print("ledger: couldn't read %s, starting a new one" % path, flush=True)

    @staticmethod
    def day_key(t):
        return time.strftime("%Y-%m-%d", time.localtime(t))

    def update(self, source, rows, now=None):
        now = now or time.time()
        key = self.day_key(now)
        lt = time.localtime(now)
        day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        days = self.days.setdefault(source, {})
        day = days.setdefault(key, {"base": {}, "cur": {}, "since": None})
        earlier = [k for k in days if k < key]
        prev = days[max(earlier)] if earlier else None
        for row in rows:
            try:
                sid, started, tokens = str(row[0]), float(row[1]), int(row[2] or 0)
            except (TypeError, ValueError, IndexError):
                continue
            if sid not in day["base"]:
                if started >= day_start:
                    day["base"][sid] = 0
                elif prev and sid in prev["cur"]:
                    day["base"][sid] = prev["cur"][sid]
                else:  # we didn't see this session before today: count from now
                    day["base"][sid] = tokens
                    day["since"] = day["since"] or now
            day["cur"][sid] = max(tokens, day["cur"].get(sid, 0))
        for old in sorted(days)[:-LEDGER_DAYS]:
            del days[old]
        self.dirty = True
        if self.path and now - self.saved_at > 60:
            self.save(now)

    def save(self, now=None):
        if not (self.path and self.dirty):
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.days, f)
            os.replace(tmp, self.path)
            self.dirty = False
            self.saved_at = now or time.time()
        except OSError as e:
            print("ledger: couldn't save: %s" % e, flush=True)

    def view(self, source):
        out = {}
        for key, day in self.days.get(source, {}).items():
            used = sum(max(0, cur - day["base"].get(sid, cur)) for sid, cur in day["cur"].items())
            out[key] = {"tokens": used, "since": day["since"]}
        return out


ledger = Ledger(os.path.join(STATE_DIR, "ledger.json") if STATE_DIR else None)

WATCH_WINDOW_S = 20   # a dashboard polls every 5 s; silent this long means nobody's looking
viewers = {"last": 0.0}


def watching():
    return time.time() - viewers["last"] < WATCH_WINDOW_S


sources = {}   # source id -> {"report": ..., "received": t}
sessions = {}  # login token -> expiry
failures = {"count": 0, "locked_until": 0.0}
lock = threading.Lock()


# ---- passwords & sessions (same scheme as garage-monitor) --------------------------

def hash_password(password, salt=None, rounds=PBKDF2_ROUNDS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), rounds).hex()
    return "pbkdf2_sha256:%d:%s:%s" % (rounds, salt, digest)


def check_password(password):
    try:
        algo, rounds, salt, _ = PASSWORD_HASH.split(":")
        if algo != "pbkdf2_sha256":
            return False
        return hmac.compare_digest(hash_password(password, salt, int(rounds)), PASSWORD_HASH)
    except ValueError:
        return False


def login(password):
    now = time.time()
    with lock:
        if now < failures["locked_until"]:
            return None, "Too many wrong passwords. Try again in a minute."
    ok = check_password(password)
    with lock:
        if not ok:
            failures["count"] += 1
            if failures["count"] >= MAX_FAILURES:
                failures["count"] = 0
                failures["locked_until"] = now + LOCKOUT_S
            return None, "Wrong password."
        failures["count"] = 0
        for t in [t for t, exp in sessions.items() if exp < now]:
            del sessions[t]
        token = secrets.token_urlsafe(32)
        sessions[token] = now + SESSION_S
        return token, None


def session_valid(token):
    if not token:
        return False
    with lock:
        exp = sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            del sessions[token]
            return False
        return True


# ---- data -----------------------------------------------------------------------------

def tab_has_renderer(tab_id):
    return bool(TAB_ID.match(tab_id)) and os.path.isfile(os.path.join(STATIC_DIR, "tabs", tab_id + ".js"))


def record(report):
    sid = str(report.get("source") or "")
    if not SOURCE_ID.match(sid):
        raise ValueError("bad source name")
    rows = ((report.get("data") or {}).get("overview") or {}).get("session_tokens")
    with lock:
        sources[sid] = {"report": report, "received": time.time()}
        if isinstance(rows, list):
            ledger.update(sid, rows)


def snapshot():
    now = time.time()
    with lock:
        for sid in [s for s, e in sources.items() if now - e["received"] > FORGET_AFTER_S]:
            del sources[sid]
        items = [{"id": sid, "kind": str(e["report"].get("kind") or "hermes")[:32],
                  "age_s": round(now - e["received"], 1), "report": e["report"],
                  "ledger": ledger.view(sid)}
                 for sid, e in sorted(sources.items())]
    # Tabs = every tab any source reports that has a renderer, in order.
    # A "global" tab (e.g. Containers) isn't about one agent: the dashboard shows
    # it from whichever source reports it, whatever agent is picked.
    tabs = {}
    for s in items:
        for t in s["report"].get("tabs") or []:
            tid = str(t.get("id", ""))
            if tid not in tabs and tab_has_renderer(tid):
                tabs[tid] = {"id": tid, "title": str(t.get("title") or tid)[:40], "order": t.get("order", 100),
                             "global": bool(t.get("global"))}
    tab_list = sorted(tabs.values(), key=lambda t: (t["order"] if isinstance(t["order"], (int, float)) else 100, t["id"]))
    return {"now": now, "sources": items, "tabs": tab_list, "garage_url": GARAGE_URL, "ui_version": UI_VERSION}


# ---- HTTP -----------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-mission-control"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, separators=(",", ":")))

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("bad size")
        obj = json.loads(self.rfile.read(length))
        if not isinstance(obj, dict):
            raise ValueError("expected an object")
        return obj

    def _bearer(self):
        auth = self.headers.get("Authorization", "")
        return auth[7:].strip() if auth.startswith("Bearer ") else ""

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or os.path.splitext(path)[1] not in STATIC_TYPES:
            return self._json(404, {"error": "not found"})
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        if path.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        self._send(200, data, ctype)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/state":
            if PASSWORD_HASH and not session_valid(self._bearer()):
                return self._json(401, {"error": "login required"})
            viewers["last"] = time.time()
            return self._json(200, snapshot())
        if path == "/api/watching":  # asked by idle collectors
            if not hmac.compare_digest(self.headers.get("X-HMC-Token", ""), TOKEN):
                return self._json(401, {"error": "bad token"})
            return self._json(200, {"watching": watching()})
        if path == "/api/health":
            return self._json(200, {"ok": True, "password": bool(PASSWORD_HASH)})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/logout":
            with lock:
                sessions.pop(self._bearer(), None)
            return self._json(200, {"ok": True})
        try:
            body = self._read_json()
        except ValueError:
            return self._json(400, {"error": "bad request"})

        if path == "/api/report":
            if not hmac.compare_digest(self.headers.get("X-HMC-Token", ""), TOKEN):
                return self._json(401, {"error": "bad token"})
            try:
                record(body)
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"ok": True, "watching": watching()})

        if path == "/api/login":
            if not PASSWORD_HASH:
                return self._json(200, {"token": "", "password": False})
            token, err = login(str(body.get("password", "")))
            if err:
                time.sleep(0.5)
                return self._json(401, {"error": err})
            return self._json(200, {"token": token, "expires_in": int(SESSION_S)})
        self._json(404, {"error": "not found"})


def serve(address):
    server = ThreadingHTTPServer((address, PORT), Handler)
    server.daemon_threads = True
    print("mission control hub listening on http://%s:%d" % (address, PORT), flush=True)
    server.serve_forever()


def main():
    if "--hash-password" in sys.argv:
        pw = sys.stdin.readline().rstrip("\n") if not sys.stdin.isatty() else getpass.getpass("Viewer password: ")
        if len(pw) < 4:
            raise SystemExit("password must be at least 4 characters")
        print(hash_password(pw))
        return
    if not TOKEN:
        raise SystemExit("HMC_TOKEN is not set; refusing to start without one")
    if not PASSWORD_HASH:
        print("warning: no viewer password set; anyone who can reach this port can view the dashboard", flush=True)
    print("token ledger: %s" % (ledger.path or "in memory only (no STATE_DIRECTORY)"), flush=True)
    import atexit
    atexit.register(ledger.save)
    addresses = [a.strip() for a in BIND.split(",") if a.strip()]
    for addr in addresses[1:]:
        threading.Thread(target=serve, args=(addr,), daemon=True).start()
    try:
        serve(addresses[0])
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
