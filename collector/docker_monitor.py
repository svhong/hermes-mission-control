#!/usr/bin/env python3
"""hermes-mission-control container monitor.

Reports the state and resource use of Docker containers to the hub. It gets
the data from the Docker Engine API, read-only: state, health checks, restart
counts, CPU, memory, network and processes. It never starts, stops or changes
anything. It shows up on the dashboard as a "Containers" tab.

Settings (environment):
  HMC_DOCKER_URL    Docker API: tcp://docker-proxy:2375 (recommended: a read-only
                    socket proxy) or unix:///var/run/docker.sock
  HMC_DOCKER_ALL    1 = every container on the host (default); 0 = only containers
                    labelled hmc.source or belonging to HMC_DOCKER_PROJECT
  HMC_DOCKER_PROJECT  compose project to show when HMC_DOCKER_ALL=0
  HMC_HUB_URL, HMC_TOKEN, HMC_INTERVAL, HMC_IDLE_INTERVAL, HMC_SHOW_TEXT,
  HMC_HEALTH_FILE   same as the collector
  HMC_SOURCE        name of this report on the hub (default: docker)

Containers can carry two labels the dashboard understands:
  hmc.source=<name>   the agent (collector source) this container belongs to
  hmc.role=<role>     agent, collector, hub, monitor, proxy...

  docker_monitor.py --once   print one report as JSON and exit
"""
import http.client
import json
import os
import socket
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hermes_db import truncate  # noqa: E402

DOCKER_URL = os.environ.get("HMC_DOCKER_URL", "unix:///var/run/docker.sock")
SHOW_ALL = os.environ.get("HMC_DOCKER_ALL", "1").strip() not in ("0", "false", "no")
PROJECT = os.environ.get("HMC_DOCKER_PROJECT", "").strip()
SOURCE = os.environ.get("HMC_SOURCE", "docker").strip() or "docker"
SHOW_TEXT = os.environ.get("HMC_SHOW_TEXT", "1").strip() not in ("0", "false", "no", "")
VERSION = "0.1.0"

TAB = {"id": "containers", "title": "Containers", "order": 40, "global": True}
HISTORY = 60              # samples of CPU/memory kept per container for the sparklines
RESTART_WINDOW_S = 3600   # "restarted N times in the last hour"
HIGH_CPU, HIGH_MEM = 90.0, 90.0


class DockerError(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


class DockerAPI:
    """Just enough of the Docker Engine API, GET only."""

    def __init__(self, url=DOCKER_URL, timeout=5):
        self.url = url
        self.timeout = timeout
        u = urllib.parse.urlparse(url)
        if u.scheme == "unix":
            self.unix_path, self.host, self.port = u.path, None, None
        elif u.scheme in ("tcp", "http"):
            self.unix_path, self.host, self.port = None, u.hostname, u.port or 2375
        else:
            raise ValueError("HMC_DOCKER_URL must start with unix:// or tcp://")

    def get(self, path):
        conn = (_UnixConnection(self.unix_path, self.timeout) if self.unix_path
                else http.client.HTTPConnection(self.host, self.port, timeout=self.timeout))
        try:
            conn.request("GET", path, headers={"Host": "docker"})
            resp = conn.getresponse()
            body = resp.read()
        except OSError as e:
            raise DockerError("can't reach Docker at %s: %s" % (self.url, e)) from None
        finally:
            conn.close()
        if resp.status != 200:
            raise DockerError("Docker said %d for %s" % (resp.status, path.split("?")[0]))
        return json.loads(body)


def _ts(value):
    """Docker's RFC 3339 time (nanoseconds, Z) -> epoch seconds; None for unset."""
    if not value or value.startswith("0001-"):
        return None
    try:
        main, _, frac = value.rstrip("Z").partition(".")
        frac = frac.split("+")[0].split("-")[0]
        import calendar
        t = calendar.timegm(time.strptime(main[:19], "%Y-%m-%dT%H:%M:%S"))
        return t + float("0." + frac[:6]) if frac else float(t)
    except ValueError:
        return None


def _mem_used(mem):
    """Memory in use like `docker stats`: usage minus reclaimable page cache."""
    usage = mem.get("usage")
    if usage is None:
        return None
    st = mem.get("stats") or {}
    cache = st.get("inactive_file", st.get("total_inactive_file", 0)) or 0
    return max(0, usage - cache) if cache < usage else usage


class Monitor:
    def __init__(self, api=None, source=SOURCE, show_all=SHOW_ALL, project=PROJECT, show_text=SHOW_TEXT):
        self.api = api or DockerAPI()
        self.source = source
        self.show_all = show_all
        self.project = project
        self.show_text = show_text
        self.prev = {}      # container id -> last raw sample for CPU/network rates
        self.history = {}   # container id -> [[t, cpu%, mem bytes], ...]
        self.restarts = {}  # container id -> [(t, restart count)] to count recent restarts
        self.engine = None

    def text(self, value, limit=160):
        return truncate(value, limit) if self.show_text and value else None

    def wanted(self, c):
        labels = c.get("Labels") or {}
        if self.show_all:
            return True
        if "hmc.source" in labels or "hmc.role" in labels:
            return True
        return bool(self.project) and labels.get("com.docker.compose.project") == self.project

    def report(self, force=False):
        now = time.time()
        started = time.monotonic()
        out = {"source": self.source, "kind": "docker", "host": socket.gethostname(), "version": VERSION,
               "collected_at": now, "tabs": [dict(TAB)], "data": {}}
        try:
            data = self.collect(now)
        except (DockerError, ValueError) as e:
            data = {"engine": {"ok": False, "error": str(e), "url": self.api.url}, "containers": [], "problems": []}
        except Exception as e:  # noqa: BLE001 - shown on the dashboard, never crash the loop
            data = {"engine": {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "url": self.api.url},
                    "containers": [], "problems": []}
        data["_ms"] = round((time.monotonic() - started) * 1000, 1)
        data["_collected_at"] = now
        out["data"]["containers"] = data
        out["collect_ms"] = data["_ms"]
        return out

    def collect(self, now):
        if self.engine is None or force_engine_refresh(self.engine, now):
            v = self.api.get("/version")
            self.engine = {"ok": True, "error": None, "url": self.api.url, "version": v.get("Version"),
                           "api_version": v.get("ApiVersion"), "os": v.get("Os"), "arch": v.get("Arch"),
                           "checked_at": now}
        listed = [c for c in self.api.get("/containers/json?all=1") if self.wanted(c)]
        rows = []
        for c in listed:
            try:
                rows.append(self.container(c, now))
            except DockerError as e:  # removed between list and inspect, or the proxy refused
                rows.append({"id": c.get("Id", "")[:12], "name": (c.get("Names") or ["?"])[0].lstrip("/"),
                             "state": c.get("State"), "error": str(e)})
        seen = {c.get("Id") for c in listed}
        for cid in [k for k in self.prev if k not in seen]:
            self.prev.pop(cid, None)
            self.history.pop(cid, None)
            self.restarts.pop(cid, None)
        rows.sort(key=lambda r: (r.get("agent") is None, r.get("agent") or "", r.get("project") or "", r["name"]))
        return {"engine": dict(self.engine), "containers": rows, "problems": problems(rows),
                "summary": summary(rows)}

    def container(self, c, now):
        cid = c["Id"]
        info = self.api.get("/containers/%s/json" % cid)
        st = info.get("State") or {}
        cfg = info.get("Config") or {}
        host = info.get("HostConfig") or {}
        labels = cfg.get("Labels") or c.get("Labels") or {}
        health = st.get("Health") or {}
        last_check = (health.get("Log") or [None])[-1] or {}
        restart_count = info.get("RestartCount", 0) or 0
        row = {
            "id": cid[:12],
            "name": (info.get("Name") or (c.get("Names") or ["?"])[0]).lstrip("/"),
            "image": truncate(cfg.get("Image") or c.get("Image") or "", 80),
            "agent": labels.get("hmc.source"),
            "role": labels.get("hmc.role"),
            "project": labels.get("com.docker.compose.project"),
            "service": labels.get("com.docker.compose.service"),
            "state": st.get("Status") or c.get("State"),
            "health": health.get("Status"),   # healthy / unhealthy / starting / None (no check)
            "health_failing_streak": health.get("FailingStreak"),
            "health_output": self.text((last_check.get("Output") or "").strip(), 200) if health.get("Status") == "unhealthy" else None,
            "started_at": _ts(st.get("StartedAt")),
            "finished_at": _ts(st.get("FinishedAt")),
            "created_at": c.get("Created"),
            "exit_code": st.get("ExitCode"),
            "oom_killed": bool(st.get("OOMKilled")),
            "error": self.text(st.get("Error")),
            "restart_count": restart_count,
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "restarting": bool(st.get("Restarting")),
            "mem_limit": host.get("Memory") or None,
            "cpu_limit": (host.get("NanoCpus") or 0) / 1e9 or None,
            "ports": sorted({"%s:%s" % (p.get("IP", ""), p.get("PublicPort")) for p in c.get("Ports") or []
                             if p.get("PublicPort")}),
        }
        # Recent restarts: Docker only keeps a running count, so remember when it grew.
        hist = self.restarts.setdefault(cid, [])
        if not hist or hist[-1][1] != restart_count:
            hist.append((now, restart_count))
        hist[:] = [h for h in hist if now - h[0] < RESTART_WINDOW_S] or hist[-1:]
        row["restarts_last_hour"] = max(0, restart_count - hist[0][1]) if len(hist) > 1 else 0
        if row["state"] == "running":
            row.update(self.stats(cid, now))
        else:
            self.prev.pop(cid, None)
        h = self.history.setdefault(cid, [])
        if row.get("cpu_pct") is not None or row.get("mem_bytes") is not None:
            h.append([round(now, 1), row.get("cpu_pct"), row.get("mem_bytes")])
            del h[:-HISTORY]
        row["history"] = list(h)
        return row

    def stats(self, cid, now):
        # one-shot: answer right away instead of waiting a second for a second sample;
        # we work out rates against our own previous sample instead.
        s = self.api.get("/containers/%s/stats?stream=false&one-shot=true" % cid)
        cpu = s.get("cpu_stats") or {}
        total = (cpu.get("cpu_usage") or {}).get("total_usage")
        system = cpu.get("system_cpu_usage")
        ncpu = cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
        nets = s.get("networks") or {}
        rx = sum(n.get("rx_bytes", 0) for n in nets.values())
        tx = sum(n.get("tx_bytes", 0) for n in nets.values())
        mem = s.get("memory_stats") or {}
        used = _mem_used(mem)
        limit = mem.get("limit")
        out = {"mem_bytes": used, "cpus": ncpu,
               "mem_pct": round(used / limit * 100, 1) if used is not None and limit else None,
               "mem_max": limit, "pids": (s.get("pids_stats") or {}).get("current"),
               "net_rx": rx, "net_tx": tx, "cpu_pct": None, "rx_rate": None, "tx_rate": None}
        prev = self.prev.get(cid)
        if prev and total is not None and system is not None and prev["total"] is not None:
            d_cpu, d_sys = total - prev["total"], system - prev["system"]
            if d_cpu >= 0 and d_sys > 0:
                out["cpu_pct"] = round(d_cpu / d_sys * ncpu * 100, 1)
            dt = now - prev["t"]
            if dt > 0 and rx >= prev["rx"] and tx >= prev["tx"]:
                out["rx_rate"] = round((rx - prev["rx"]) / dt)
                out["tx_rate"] = round((tx - prev["tx"]) / dt)
        self.prev[cid] = {"t": now, "total": total, "system": system, "rx": rx, "tx": tx}
        return out


def force_engine_refresh(engine, now):
    return now - engine.get("checked_at", 0) > 600


def problems(rows):
    out = []
    for r in rows:
        name = r["name"]
        if r.get("error") and r.get("state") is None:
            out.append({"level": "warning", "container": name, "text": "%s: %s" % (name, r["error"])})
            continue
        if r.get("health") == "unhealthy":
            out.append({"level": "critical", "container": name,
                        "text": "%s is unhealthy (health check failed %s times in a row)" % (name, r.get("health_failing_streak") or "several")})
        if r.get("restarting"):
            out.append({"level": "critical", "container": name, "text": "%s keeps restarting" % name})
        elif r.get("restarts_last_hour", 0) >= 3:
            out.append({"level": "warning", "container": name,
                        "text": "%s restarted %d times in the last hour" % (name, r["restarts_last_hour"])})
        if r.get("oom_killed"):
            out.append({"level": "critical", "container": name, "text": "%s was killed for using too much memory" % name})
        if r.get("state") in ("exited", "dead") and r.get("restart_policy") not in ("no", "") and r.get("exit_code"):
            out.append({"level": "critical", "container": name,
                        "text": "%s stopped with exit code %s" % (name, r["exit_code"])})
        elif r.get("state") in ("exited", "dead") and r.get("agent") and r.get("role") in ("agent", "collector", "hub"):
            out.append({"level": "warning", "container": name, "text": "%s is stopped" % name})
        if (r.get("mem_pct") or 0) >= HIGH_MEM and r.get("mem_limit"):
            out.append({"level": "warning", "container": name,
                        "text": "%s is using %.0f%% of its memory limit" % (name, r["mem_pct"])})
        if (r.get("cpu_pct") or 0) >= HIGH_CPU * (r.get("cpu_limit") or r.get("cpus") or 1):
            out.append({"level": "warning", "container": name, "text": "%s is using %.0f%% CPU" % (name, r["cpu_pct"])})
    return out


def summary(rows):
    s = {"total": len(rows), "running": 0, "stopped": 0, "unhealthy": 0, "healthy": 0,
         "cpu_pct": 0.0, "mem_bytes": 0}
    for r in rows:
        if r.get("state") == "running":
            s["running"] += 1
        else:
            s["stopped"] += 1
        if r.get("health") == "unhealthy":
            s["unhealthy"] += 1
        elif r.get("health") == "healthy":
            s["healthy"] += 1
        s["cpu_pct"] += r.get("cpu_pct") or 0
        s["mem_bytes"] += r.get("mem_bytes") or 0
    s["cpu_pct"] = round(s["cpu_pct"], 1)
    return s


def main():
    import collector as base
    mon = Monitor()
    if "--once" in sys.argv:
        mon.report()
        time.sleep(1)  # a second sample so CPU and network rates show up
        print(json.dumps(mon.report(), indent=2, default=str))
        return
    if not base.TOKEN:
        raise SystemExit("HMC_TOKEN is not set")
    print("container monitor: reading %s, reporting to %s as %r (%s)" % (
        DOCKER_URL, base.HUB_URL, SOURCE, "all containers" if SHOW_ALL else "labelled containers only"), flush=True)
    base.run(mon.report, base.send, base.check_watching)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
