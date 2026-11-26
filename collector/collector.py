#!/usr/bin/env python3
"""hermes-mission-control collector.

Runs as the Linux user that owns Hermes, reads Hermes's state.db read-only,
and sends a small summary (counts, times, tokens, costs, short titles; never
message text) to the hub every few seconds. It doesn't listen on the network.

Settings (see /etc/hermes-mission-control/collector.env):
  HMC_HUB_URL     hub address, e.g. http://127.0.0.1:8788
  HMC_TOKEN       shared secret the hub expects
  HMC_DB          path to state.db (default: ~/.hermes/state.db)
  HMC_SOURCE      name for this Hermes on the dashboard (default: hermes)
  HMC_INTERVAL    seconds between reports (default 5)
  HMC_SHOW_TEXT   1 to include short titles, activity and task goals; 0 for numbers only

  collector.py --once   print one report as JSON and exit (for checking)
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_db import HermesDB  # noqa: E402
from tabs import Context, discover  # noqa: E402

HUB_URL = os.environ.get("HMC_HUB_URL", "http://127.0.0.1:8788").rstrip("/")
TOKEN = os.environ.get("HMC_TOKEN", "")
DB_PATH = os.path.expanduser(os.environ.get("HMC_DB", "~/.hermes/state.db"))
SOURCE = os.environ.get("HMC_SOURCE", "hermes").strip() or "hermes"
INTERVAL = max(1.0, float(os.environ.get("HMC_INTERVAL", "5")))
SHOW_TEXT = os.environ.get("HMC_SHOW_TEXT", "1").strip() not in ("0", "false", "no", "")
VERSION = "0.1.0"


class Collector:
    def __init__(self, db_path=DB_PATH, source=SOURCE, show_text=SHOW_TEXT):
        self.db_path = db_path
        self.source = source
        self.show_text = show_text
        self.tabs = discover()
        self.cache = {}  # tab id -> (collected_at, data)

    def report(self, force=False):
        ctx = Context(show_text=self.show_text)
        started = time.monotonic()
        out = {
            "source": self.source,
            "kind": "hermes",
            "host": socket.gethostname(),
            "version": VERSION,
            "collected_at": ctx.now,
            "db": {"ok": False, "error": None, "schema_version": None, "immutable": False},
            "tabs": [],
            "data": {},
        }
        try:
            with HermesDB(self.db_path) as db:
                out["db"].update(ok=True, schema_version=db.schema_version, immutable=db.immutable)
                for mod in self.tabs:
                    tid = mod.TAB["id"]
                    interval = getattr(mod, "INTERVAL", INTERVAL)
                    cached = self.cache.get(tid)
                    if force or cached is None or ctx.now - cached[0] >= interval - 0.5:
                        t0 = time.monotonic()
                        try:
                            data = mod.collect(db, ctx)
                        except Exception as e:  # noqa: BLE001 - one bad tab mustn't stop the others
                            data = {"_error": "%s: %s" % (type(e).__name__, e)}
                        data["_ms"] = round((time.monotonic() - t0) * 1000, 1)
                        cached = (ctx.now, data)
                        self.cache[tid] = cached
                    out["data"][tid] = dict(cached[1], _collected_at=cached[0])
        except Exception as e:  # noqa: BLE001 - shown on the dashboard
            out["db"]["error"] = "%s: %s" % (type(e).__name__, e)
        out["tabs"] = [dict(m.TAB) for m in self.tabs]
        out["collect_ms"] = round((time.monotonic() - started) * 1000, 1)
        return out


def send(report):
    body = json.dumps(report, separators=(",", ":")).encode()
    req = urllib.request.Request(HUB_URL + "/api/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-HMC-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()


def main():
    collector = Collector()
    if "--once" in sys.argv:
        print(json.dumps(collector.report(force=True), indent=2, default=str))
        return
    if not TOKEN:
        raise SystemExit("HMC_TOKEN is not set")
    print("collector: reading %s, reporting to %s every %ss as %r"
          % (DB_PATH, HUB_URL, INTERVAL, SOURCE), flush=True)
    last_error = None
    while True:
        t0 = time.monotonic()
        try:
            send(collector.report())
            if last_error:
                print("collector: hub reachable again", flush=True)
            last_error = None
        except (urllib.error.URLError, OSError) as e:
            msg = str(e)
            if msg != last_error:  # log each new problem once, not every cycle
                print("collector: can't reach hub: %s" % msg, flush=True)
            last_error = msg
        time.sleep(max(0.5, INTERVAL - (time.monotonic() - t0)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
