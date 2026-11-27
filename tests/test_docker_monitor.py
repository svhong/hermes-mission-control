"""Run with: python3 -m unittest discover -s tests"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))

import docker_monitor  # noqa: E402
from docker_monitor import DockerAPI, Monitor, _ts  # noqa: E402


def container(cid, name, state="running", labels=None, health=None, restarts=0, exit_code=0,
              policy="unless-stopped", mem_limit=0, oom=False, restarting=False):
    return {
        "list": {"Id": cid, "Names": ["/" + name], "Image": "img:" + name, "State": state,
                 "Labels": labels or {}, "Created": 1_700_000_000, "Ports": []},
        "inspect": {"Id": cid, "Name": "/" + name, "RestartCount": restarts,
                    "Config": {"Image": "img:" + name, "Labels": labels or {}},
                    "HostConfig": {"RestartPolicy": {"Name": policy}, "Memory": mem_limit, "NanoCpus": 0},
                    "State": {"Status": state, "StartedAt": "2026-11-27T05:00:00.123456789Z",
                              "FinishedAt": "0001-01-01T00:00:00Z", "ExitCode": exit_code,
                              "OOMKilled": oom, "Restarting": restarting, "Error": "",
                              **({"Health": {"Status": health, "FailingStreak": 4 if health == "unhealthy" else 0,
                                             "Log": [{"Output": "curl: connection refused\n"}]}} if health else {})}},
    }


class FakeDocker:
    """A tiny Docker Engine API: enough for the monitor, GET only."""

    def __init__(self):
        self.containers = {}
        self.cpu = {}     # id -> (total_usage, system_usage)
        self.net = {}
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.requests.append(self.path)
                path = self.path.split("?")[0]
                parts = path.strip("/").split("/")
                if path == "/version":
                    return self.ok({"Version": "29.9.3", "ApiVersion": "1.51", "Os": "linux", "Arch": "amd64"})
                if path == "/containers/json":
                    return self.ok([c["list"] for c in fake.containers.values()])
                if len(parts) == 3 and parts[0] == "containers" and parts[1] in fake.containers:
                    cid = parts[1]
                    if parts[2] == "json":
                        return self.ok(fake.containers[cid]["inspect"])
                    if parts[2] == "stats":
                        total, system = fake.cpu.get(cid, (0, 0))
                        rx, tx = fake.net.get(cid, (0, 0))
                        return self.ok({
                            "cpu_stats": {"cpu_usage": {"total_usage": total}, "system_cpu_usage": system, "online_cpus": 4},
                            "memory_stats": {"usage": 150 * 2**20, "limit": 200 * 2**20, "stats": {"inactive_file": 50 * 2**20}},
                            "pids_stats": {"current": 7},
                            "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
                        })
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                fake.requests.append("POST " + self.path)
                self.send_response(403)
                self.end_headers()

            def ok(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "tcp://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, c):
        self.containers[c["list"]["Id"]] = c

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DockerMonitorTest(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.addCleanup(self.docker.close)
        self.docker.add(container("a" * 64, "hermes", labels={"hmc.source": "hermes", "hmc.role": "agent"}))
        self.docker.add(container("b" * 64, "hmc-hub", labels={"hmc.role": "hub"}, health="unhealthy", mem_limit=128 * 2**20))
        self.docker.add(container("c" * 64, "postgres", state="exited", exit_code=137, policy="always"))
        self.docker.cpu["a" * 64] = (1_000_000_000, 100_000_000_000)
        self.docker.net["a" * 64] = (1000, 500)

    def monitor(self, **kw):
        return Monitor(DockerAPI(self.docker.url), source="docker", show_all=kw.pop("show_all", True), **kw)

    def test_report_shape_and_rates(self):
        mon = self.monitor()
        clock = [1000.0]
        orig = docker_monitor.time.time
        docker_monitor.time.time = lambda: clock[0]
        self.addCleanup(setattr, docker_monitor.time, "time", orig)
        r1 = mon.report()
        self.assertEqual(r1["kind"], "docker")
        self.assertEqual(r1["tabs"][0]["id"], "containers")
        self.assertTrue(r1["tabs"][0]["global"])
        d1 = r1["data"]["containers"]
        self.assertTrue(d1["engine"]["ok"])
        self.assertEqual(d1["engine"]["version"], "29.9.3")
        hermes = next(c for c in d1["containers"] if c["name"] == "hermes")
        self.assertIsNone(hermes["cpu_pct"])          # needs a second sample
        self.assertEqual(hermes["mem_bytes"], 100 * 2**20)   # page cache not counted
        self.assertEqual(hermes["agent"], "hermes")
        self.assertEqual(hermes["pids"], 7)
        # 0.5 s of CPU over 10 s of system time on 4 CPUs = 20%
        self.docker.cpu["a" * 64] = (1_500_000_000, 110_000_000_000)
        self.docker.net["a" * 64] = (11000, 2500)
        clock[0] += 10
        d2 = mon.report()["data"]["containers"]
        hermes = next(c for c in d2["containers"] if c["name"] == "hermes")
        self.assertEqual(hermes["cpu_pct"], 20.0)
        self.assertEqual(hermes["rx_rate"], 1000)
        self.assertEqual(hermes["tx_rate"], 200)
        self.assertEqual(len(hermes["history"]), 2)
        self.assertEqual(d2["summary"]["running"], 2)
        self.assertEqual(d2["summary"]["total"], 3)
        self.assertEqual(d2["summary"]["unhealthy"], 1)
        # Agent containers first.
        self.assertEqual(d2["containers"][0]["name"], "hermes")
        json.dumps(d2)  # JSON-safe

    def test_problems(self):
        d = self.monitor().report()["data"]["containers"]
        texts = " | ".join(p["text"] for p in d["problems"])
        self.assertIn("hmc-hub is unhealthy", texts)
        self.assertIn("postgres stopped with exit code 137", texts)
        hub = next(c for c in d["containers"] if c["name"] == "hmc-hub")
        self.assertEqual(hub["health_output"], "curl: connection refused")

    def test_restarts_in_the_last_hour(self):
        self.docker.add(container("d" * 64, "flaky", restarts=2))
        mon = self.monitor()
        mon.report()
        self.docker.containers["d" * 64]["inspect"]["RestartCount"] = 6
        d = mon.report()["data"]["containers"]
        flaky = next(c for c in d["containers"] if c["name"] == "flaky")
        self.assertEqual(flaky["restarts_last_hour"], 4)
        self.assertTrue(any("flaky restarted 4 times" in p["text"] for p in d["problems"]))

    def test_numbers_only_drops_health_output(self):
        d = self.monitor(show_text=False).report()["data"]["containers"]
        hub = next(c for c in d["containers"] if c["name"] == "hmc-hub")
        self.assertIsNone(hub["health_output"])

    def test_only_labelled_containers(self):
        d = self.monitor(show_all=False).report()["data"]["containers"]
        self.assertEqual(sorted(c["name"] for c in d["containers"]), ["hermes", "hmc-hub"])

    def test_unreachable_docker_is_reported_not_raised(self):
        mon = Monitor(DockerAPI("tcp://127.0.0.1:1"), source="docker")
        d = mon.report()["data"]["containers"]
        self.assertFalse(d["engine"]["ok"])
        self.assertIn("can't reach Docker", d["engine"]["error"])

    def test_removed_container_is_forgotten(self):
        mon = self.monitor()
        mon.report()
        del self.docker.containers["a" * 64]
        mon.report()
        self.assertNotIn("a" * 64, mon.history)

    def test_read_only(self):
        self.monitor().report()
        self.assertFalse([r for r in self.docker.requests if r.startswith("POST")])

    def test_timestamps(self):
        self.assertIsNone(_ts("0001-01-01T00:00:00Z"))
        self.assertAlmostEqual(_ts("2026-11-27T05:00:00.5Z") % 60, 0.5)


class HealthFileTest(unittest.TestCase):
    def test_mark_healthy_touches_file(self):
        import tempfile
        import collector
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ok")
            collector.mark_healthy(path)
            self.assertTrue(os.path.exists(path))
            collector.mark_healthy("")  # disabled: no error


if __name__ == "__main__":
    unittest.main()
