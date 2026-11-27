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
  HMC_INTERVAL    seconds between reports while a dashboard is open (default 5)
  HMC_IDLE_INTERVAL  seconds between reports when nobody is watching (default 60);
                  while idle it asks the hub every HMC_INTERVAL seconds whether
                  someone opened the dashboard (no database read for that)
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
from machine import Machine  # noqa: E402
from tabs import Context, discover  # noqa: E402

HUB_URL = os.environ.get("HMC_HUB_URL", "http://127.0.0.1:8788").rstrip("/")
TOKEN = os.environ.get("HMC_TOKEN", "")
DB_PATH = os.path.expanduser(os.environ.get("HMC_DB", "~/.hermes/state.db"))
SOURCE = os.environ.get("HMC_SOURCE", "hermes").strip() or "hermes"
INTERVAL = max(1.0, float(os.environ.get("HMC_INTERVAL", "5")))
IDLE_INTERVAL = max(INTERVAL, float(os.environ.get("HMC_IDLE_INTERVAL", "60")))
SHOW_TEXT = os.environ.get("HMC_SHOW_TEXT", "1").strip() not in ("0", "false", "no", "")
VERSION = "0.1.0"


class Collector:
    def __init__(self, db_path=DB_PATH, source=SOURCE, show_text=SHOW_TEXT):
        self.db_path = db_path
        self.source = source
        self.show_text = show_text
        self.tabs = discover()
        self.cache = {}  # tab id -> (collected_at, data)
        self.machine = Machine(gateway_pids=_gateway_pids)

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
        # The machine's health doesn't depend on Hermes's database.
        t0 = time.monotonic()
        try:
            out["machine"] = self.machine.sample(ctx.now)
            out["machine"]["_ms"] = round((time.monotonic() - t0) * 1000, 1)
        except Exception as e:  # noqa: BLE001 - shown on the dashboard
            out["machine"] = {"_error": "%s: %s" % (type(e).__name__, e)}
        out["collect_ms"] = round((time.monotonic() - started) * 1000, 1)
        return out


def _gateway_pids():
    from tabs.overview import scan_gateways
    return (scan_gateways() or {}).keys()


def _watching(resp):
    """The hub says whether a dashboard is open. Older hubs don't: assume yes."""
    try:
        return bool(json.loads(resp.read() or b"{}").get("watching", True))
    except ValueError:
        return True


def send(report):
    """Send a report; returns whether someone is watching the dashboard."""
    body = json.dumps(report, separators=(",", ":")).encode()
    req = urllib.request.Request(HUB_URL + "/api/report", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-HMC-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return _watching(resp)


def check_watching():
    """Ask the hub whether a dashboard is open. Doesn't touch Hermes's database."""
    req = urllib.request.Request(HUB_URL + "/api/watching", headers={"X-HMC-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return _watching(resp)


def run(collect, send, check, sleep=time.sleep, clock=time.monotonic, cycles=None, log=print):
    """Main loop. Reads every INTERVAL seconds while a dashboard is open and every
    IDLE_INTERVAL seconds otherwise; while idle, checks every INTERVAL seconds
    whether someone opened one, and reads right away if so."""
    watching = True
    last_sent = None
    last_error = None
    n = 0
    while cycles is None or n < cycles:
        n += 1
        now = clock()
        every = INTERVAL if watching else IDLE_INTERVAL
        try:
            if last_sent is None or now - last_sent >= every - 0.05:
                report = collect()
                report["interval_s"] = every
                was = watching
                last_sent = now
                watching = send(report)
                if watching != was:
                    log("collector: %s" % ("dashboard open, reading every %gs" % INTERVAL if watching
                                           else "nobody watching, reading every %gs" % IDLE_INTERVAL), flush=True)
            elif check():
                watching = True
                last_sent = None  # someone just opened the dashboard: read now
                log("collector: dashboard open, reading every %gs" % INTERVAL, flush=True)
                continue
            if last_error:
                log("collector: hub reachable again", flush=True)
            last_error = None
        except (urllib.error.URLError, OSError) as e:
            msg = str(e)
            if msg != last_error:  # log each new problem once, not every cycle
                log("collector: can't reach hub: %s" % msg, flush=True)
            last_error = msg
        sleep(max(0.5, INTERVAL - (clock() - now)))


def main():
    collector = Collector()
    if "--once" in sys.argv:
        collector.machine.sample()
        time.sleep(1)  # so CPU, network and disk rates have something to compare against
        print(json.dumps(collector.report(force=True), indent=2, default=str))
        return
    if not TOKEN:
        raise SystemExit("HMC_TOKEN is not set")
    print("collector: reading %s, reporting to %s as %r: every %gs while a dashboard is open, "
          "every %gs otherwise" % (DB_PATH, HUB_URL, SOURCE, INTERVAL, IDLE_INTERVAL), flush=True)
    run(collector.report, send, check_watching)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
