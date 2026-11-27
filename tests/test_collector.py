"""Run with: python3 -m unittest discover -s tests"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "collector"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fake_hermes  # noqa: E402
from collector import Collector  # noqa: E402
from hermes_db import HermesDB, parse_list, pick_text  # noqa: E402


class CollectorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.tmp.name, "state.db")
        fake = fake_hermes.Fake(cls.db_path)
        fake.populate()
        fake.db.close()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def report(self, path=None, show_text=True):
        return Collector(path or self.db_path, "test", show_text).report(force=True)

    def test_every_tab_and_section_works(self):
        r = self.report()
        self.assertTrue(r["db"]["ok"], r["db"])
        self.assertEqual(r["db"]["schema_version"], fake_hermes.SCHEMA_VERSION)
        self.assertEqual([t["id"] for t in r["tabs"]], ["overview", "scheduled", "agents"])
        for tab, data in r["data"].items():
            self.assertNotIn("_error", data, tab)
            for name, section in data.items():
                if isinstance(section, dict):
                    self.assertNotIn("_error", section, "%s.%s" % (tab, name))

    def test_no_private_text_leaks(self):
        """Message text, prompts, reasoning, chat ids, payloads must never be sent."""
        for show_text in (True, False):
            blob = json.dumps(self.report(show_text=show_text))
            self.assertNotIn(fake_hermes.PRIVATE_MARKER, blob)

    def test_numbers_only_mode_drops_titles(self):
        r = self.report(show_text=False)
        for row in r["data"]["overview"]["recent"]:
            self.assertIsNone(row["title"])
            self.assertIsNone(row["activity"])
        for d in r["data"]["agents"]["delegations"]:
            self.assertIsNone(d["goal"])

    def test_subagents_are_nested_under_conversations(self):
        tree = self.report()["data"]["agents"]["tree"]
        self.assertTrue(tree["conversations"])
        kids = [k for c in tree["conversations"] for k in c["children"]]
        self.assertTrue(kids)
        self.assertTrue(all(k["kind"] == "subagent" for k in kids))
        # Live fake conversations have running subagents.
        self.assertTrue(any(c["subtree"]["running"] for c in tree["conversations"]))

    def test_compression_continuations_fold_into_one_conversation(self):
        tree = self.report()["data"]["agents"]["tree"]
        ids = [c["id"] for c in tree["conversations"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(c.get("continuations") for c in tree["conversations"]))

    def test_survives_missing_tables_and_columns(self):
        """A Hermes update that drops or renames things mustn't break the collector."""
        path = os.path.join(self.tmp.name, "older.db")
        src = sqlite3.connect(self.db_path)
        conn = sqlite3.connect(path)
        src.backup(conn)
        src.close()
        conn.execute("DROP INDEX idx_sessions_effective_activity")
        for t in ("resumable_subagents", "delivery_obligations", "worker_executions", "gateway_heartbeats",
                  "session_model_usage", "async_delegations", "session_admissions"):
            conn.execute("DROP TABLE %s" % t)
        for c in ("model_config", "last_activity_at", "profile_name", "title", "tool_names", "actual_cost_usd"):
            conn.execute("ALTER TABLE sessions DROP COLUMN %s" % c)
        conn.commit()
        conn.close()
        r = self.report(path)
        self.assertTrue(r["db"]["ok"])
        for tab, data in r["data"].items():
            for name, section in data.items():
                if isinstance(section, dict):
                    self.assertNotIn("_error", section, "%s.%s" % (tab, name))

    def test_missing_database_is_reported_not_raised(self):
        r = self.report(os.path.join(self.tmp.name, "nope.db"))
        self.assertFalse(r["db"]["ok"])
        self.assertIn("not found", r["db"]["error"])

    def test_opens_read_only(self):
        with HermesDB(self.db_path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.conn.execute("DELETE FROM sessions")


class LinkingTest(unittest.TestCase):
    """Hand-built cases mirroring what a real hermes-box database contains."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "state.db")
        conn = sqlite3.connect(self.path)
        with open(fake_hermes.SCHEMA) as f:
            conn.executescript(f.read())
        self.conn = conn
        self.now = __import__("time").time()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def session(self, sid, source="telegram", parent=None, started=0, ended=None, end_reason=None,
                title=None, delegate=False, tool_names=None):
        cfg = json.dumps({"_delegate_from": parent}) if delegate else "{}"
        self.conn.execute(
            "INSERT INTO sessions (id, source, model, model_config, parent_session_id, started_at, ended_at, "
            "end_reason, title, last_activity_at, tool_names) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, source, "m", cfg, parent, self.now - started, ended and self.now - ended, end_reason, title,
             self.now - (ended or 0), tool_names))

    def tree(self):
        self.conn.commit()
        return Collector(self.path, "t", True).report(force=True)["data"]["agents"]["tree"]["conversations"]

    def test_session_reset_starts_a_new_conversation(self):
        self.session("old", started=9000, ended=5000, end_reason="session_reset", title="Old chat")
        self.session("new", parent="old", started=5000, title="New chat")
        self.session("sub", source="subagent", parent="new", started=100, ended=50, end_reason="agent_close",
                     delegate=True)
        convs = self.tree()
        self.assertEqual([c["id"] for c in convs], ["new"])
        self.assertEqual(convs[0]["title"], "New chat")
        self.assertEqual(convs[0]["continuations"], 0)

    def test_compression_continuation_is_folded(self):
        self.session("first", started=9000, ended=5000, end_reason="context_compression", title="Long chat")
        self.session("second", parent="first", started=5000)
        self.session("sub", source="subagent", parent="second", started=100, ended=50, delegate=True)
        convs = self.tree()
        self.assertEqual([c["id"] for c in convs], ["first"])
        self.assertEqual(convs[0]["continuations"], 1)

    def test_subagent_goal_matched_by_timing(self):
        self.session("chat", started=9000)
        # Two subagents started 20 ms apart; records tell them apart by when they finished.
        self.session("a", source="subagent", parent="chat", started=300.00, ended=100, delegate=True,
                     title="Subagent: CONTEXT: #6", tool_names="67b07f7ac485d7bdde8780b46861f2f9c5a3ddb9db0232bd")
        self.session("b", source="subagent", parent="chat", started=299.98, ended=150, delegate=True,
                     title="Subagent: CONTEXT: #7")
        for rid, created, updated, goal in (("sa-0", 299.95, 100.01, "Goal A"), ("sa-1", 299.94, 150.01, "Goal B")):
            self.conn.execute("INSERT INTO resumable_subagents VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (rid, "completed", 0, None, None, None, json.dumps({"goal": goal}),
                               self.now - created, self.now - updated, None))
        subs = {s["id"]: s for s in self.tree()[0]["children"]}
        self.assertEqual(subs["a"]["goal"], "Goal A")
        self.assertEqual(subs["b"]["goal"], "Goal B")
        self.assertEqual(subs["a"]["title"], "Goal A")  # generic title replaced
        self.assertEqual(subs["a"]["tool_names"], [])   # a hash, not tool names


class CurrentTurnTest(unittest.TestCase):
    def test_counts_only_since_the_last_user_message(self):
        import time
        from tabs.overview import current_turn
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.db")
        conn = sqlite3.connect(path)
        with open(fake_hermes.SCHEMA) as f:
            conn.executescript(f.read())
        now = time.time()
        conn.execute("INSERT INTO sessions (id, source, started_at) VALUES ('s', 'telegram', ?)", (now - 86400,))
        msgs = [("user", None, 9000, 10), ("tool", "web_search", 8990, 10),        # yesterday's task
                ("user", None, 300, 50), ("assistant", None, 290, 400),             # today's task
                ("tool", "terminal", 280, 100), ("tool", "terminal", 200, 100), ("tool", "patch", 100, 100)]
        for role, tool, ago, tokens in msgs:
            conn.execute("INSERT INTO messages (session_id, role, content, tool_name, timestamp, token_count) "
                         "VALUES ('s', ?, 'x', ?, ?, ?)", (role, tool, now - ago, tokens))
        conn.commit()
        conn.close()
        with HermesDB(path) as db:
            turn = current_turn(db, "s")
        self.assertAlmostEqual(turn["started_at"], now - 300, delta=1)
        self.assertEqual(turn["tool_calls"], 3)
        self.assertEqual(turn["messages"], 5)
        self.assertEqual(turn["tokens"], 750)
        self.assertEqual(turn["tools"], [("terminal", 2), ("patch", 1)])


class GatewayProcessTest(unittest.TestCase):
    def spawn(self, *args):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *args])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        return p

    def test_finds_gateway_run_processes_only(self):
        import time
        from tabs.overview import gateway_processes
        gw = self.spawn("gateway", "run")
        other = self.spawn("gateway", "status")
        time.sleep(0.3)
        procs = gateway_processes()
        self.assertIn(gw.pid, procs)
        self.assertNotIn(other.pid, procs)
        self.assertLess(abs(procs[gw.pid] - time.time()), 5)

    def test_running_gateway_with_stale_heartbeat_is_up(self):
        """Like hermes-box: idle gateway, last heartbeat hours ago, pid not matching."""
        import time
        from tabs import Context
        from tabs.overview import gateways, warnings
        gw = self.spawn("gateway", "run")
        time.sleep(0.3)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.db")
        conn = sqlite3.connect(path)
        with open(fake_hermes.SCHEMA) as f:
            conn.executescript(f.read())
        now = time.time()
        conn.execute("INSERT INTO gateway_heartbeats VALUES ('a', 999999, ?, ?, '', '')", (now - 50000, now - 14659))
        conn.commit()
        conn.close()
        with HermesDB(path) as db:
            rows = gateways(db, Context(now))
            self.assertTrue(rows[0]["alive"])
            self.assertEqual(rows[0]["pid"], gw.pid)
            self.assertEqual(warnings(db, Context(now)), [])
        gw.kill()
        gw.wait()
        with HermesDB(path) as db:
            self.assertFalse(any(g["alive"] for g in gateways(db, Context(now))))
            self.assertIn("isn't running", warnings(db, Context(now))[0]["text"])


    def test_gateway_in_a_container(self):
        """Hermes in Docker: the heartbeat has the container's hostname and the pid
        inside the container; the host sees another pid. Show one gateway, not two."""
        import time
        from unittest import mock
        from tabs import Context
        from tabs import overview
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.db")
        conn = sqlite3.connect(path)
        with open(fake_hermes.SCHEMA) as f:
            conn.executescript(f.read())
        now = time.time()
        started = now - 3000
        # Current run (idle, stale heartbeat) and a run from before a container restart,
        # which had the same inner pid.
        conn.execute("INSERT INTO gateway_heartbeats VALUES ('a', 7, ?, ?, '', 'f3c9a1b2d4e5')",
                     (started + 2, now - 1200))
        conn.execute("INSERT INTO gateway_heartbeats VALUES ('b', 7, ?, ?, '', '0a1b2c3d4e5f')",
                     (now - 9000, now - 3100))
        conn.commit()
        conn.close()
        scan = {176167: {"start": started, "inner_pid": 7}}
        with mock.patch.object(overview, "scan_gateways", return_value=scan), HermesDB(path) as db:
            rows = overview.gateways(db, Context(now))
            alive = [g for g in rows if g["alive"]]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0]["pid"], 176167)
            self.assertEqual(alive[0]["uptime_s"], 3000)
            self.assertEqual(overview.warnings(db, Context(now)), [])
        with mock.patch.object(overview, "scan_gateways", return_value={}), HermesDB(path) as db:
            self.assertFalse(any(g["alive"] for g in overview.gateways(db, Context(now))))

    def test_inner_pid_outside_a_container(self):
        from tabs.overview import _inner_pid
        self.assertIsNone(_inner_pid(os.getpid()))


class IdleLoopTest(unittest.TestCase):
    """The collector reads often only while a dashboard is open (simulated clock)."""

    def simulate(self, seconds, viewer_from=None):
        import collector as col
        t = {"now": 0.0}
        reads, checks = [], []
        watching = lambda: viewer_from is not None and t["now"] >= viewer_from  # noqa: E731

        def collect():
            reads.append(t["now"])
            return {}

        def send(report):
            return watching()

        def check():
            checks.append(t["now"])
            return watching()

        def sleep(s):
            t["now"] += s

        col.run(collect, send, check, sleep=sleep, clock=lambda: t["now"],
                cycles=int(seconds / col.INTERVAL) + 5, log=lambda *a, **k: None)
        return [r for r in reads if r <= seconds], checks

    def test_idle_reads_about_once_a_minute(self):
        reads, checks = self.simulate(600)
        self.assertLessEqual(len(reads), 12)   # vs 120 when always reading every 5 s
        self.assertGreater(len(checks), 100)   # cheap "anyone watching?" pings instead

    def test_reads_right_away_when_someone_opens_the_dashboard(self):
        reads, _ = self.simulate(300, viewer_from=200)
        after = [r for r in reads if r >= 200]
        self.assertLessEqual(after[0] - 200, 5.5)       # within one check
        self.assertGreaterEqual(len(after), 15)          # then every 5 s again


class ScheduledTest(unittest.TestCase):
    def test_describe_schedules(self):
        from tabs.scheduled import describe
        self.assertEqual(describe("0 8 * * *"), "Daily 08:00")
        self.assertEqual(describe("30 8 * * *"), "Daily 08:30")
        self.assertEqual(describe("0 9 * * 1,4"), "Mon & Thu 09:00")
        self.assertEqual(describe("0 7 * * 1-5"), "Weekdays 07:00")
        self.assertEqual(describe("0 20 * * 0"), "Sun 20:00")
        self.assertEqual(describe("0 20 * * 7"), "Sun 20:00")
        self.assertEqual(describe("0 9 * * 1,3,5"), "Mon, Wed, Fri 09:00")
        self.assertEqual(describe("15 * * * *"), "Hourly at :15")
        self.assertEqual(describe("*/10 * * * *"), "Every 10 min")
        self.assertEqual(describe("0 6 1 * *"), "Monthly on day 1 06:00")
        self.assertEqual(describe("0 6 1 1 *"), "0 6 1 1 *")          # unusual: shown as is
        self.assertEqual(describe("@daily"), "@daily")

    def test_run_name(self):
        from tabs.scheduled import run_name
        self.assertEqual(run_name("Israel morning briefing \u00b7 Nov 26 08:05"), "Israel morning briefing")
        self.assertEqual(run_name(None), "")

    def test_jobs_from_fake_hermes(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.db")
        fake = fake_hermes.Fake(path)
        fake.populate()
        fake.db.close()
        data = Collector(path, "t", True).report(force=True)["data"]["scheduled"]
        jobs = {j["name"]: j for j in data["jobs"]["jobs"]}
        self.assertEqual(set(jobs), {j[1] for j in fake_hermes.JOBS})
        self.assertEqual(jobs["Price tracker pilot"]["status"], "failing")
        self.assertEqual(jobs["Weekly backup check"]["status"], "paused")
        self.assertIsNone(jobs["Weekly backup check"]["next_run_at"])
        self.assertEqual(jobs["Combat sports briefing"]["schedule"], "Mon & Thu 09:00")
        self.assertEqual(jobs["Morning news digest"]["deliver"], "telegram")   # chat id dropped
        self.assertTrue(jobs["Morning news digest"]["runs"])                   # runs matched by title
        self.assertTrue(any(p["text"].startswith("Price tracker pilot has failed") for p in data["problems"]))
        self.assertTrue(data["scheduler"]["ticking"])

    def test_overdue_and_missing_jobs_file(self):
        import time
        from tabs import Context
        from tabs.scheduled import jobs as jobs_section
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "state.db")
        conn = sqlite3.connect(path)
        with open(fake_hermes.SCHEMA) as f:
            conn.executescript(f.read())
        conn.close()
        cron = os.path.join(tmp.name, "cron")
        with HermesDB(path) as db:
            self.assertFalse(jobs_section(db, Context(), cron)["jobs_file"])
            os.makedirs(cron)
            with open(os.path.join(cron, "jobs.json"), "w") as f:
                json.dump({"jobs": [{"id": "j1", "name": "Late job", "enabled": True,
                                     "schedule": {"kind": "cron", "expr": "0 8 * * *"},
                                     "next_run_at": time.time() - 3600, "last_status": "ok"}]}, f)
            job = jobs_section(db, Context(), cron)["jobs"][0]
        self.assertTrue(job["overdue"])
        self.assertEqual(job["status"], "overdue")


class HelpersTest(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(parse_list('["a", "b", "a"]'), ["a", "b"])
        self.assertEqual(parse_list("x, y"), ["x", "y"])
        self.assertEqual(parse_list(None), [])
        self.assertEqual(parse_list("67b07f7ac485d7bdde8780b46861f2f9c5a3ddb9db0232bdd469b20c4edc"), [])

    def test_pick_text_truncates_and_ignores_other_keys(self):
        self.assertEqual(pick_text('{"goal": "short"}'), "short")
        self.assertEqual(pick_text('{"task": {"goal": "nested"}}'), "nested")
        self.assertIsNone(pick_text('{"context": "secret"}'))
        self.assertEqual(len(pick_text(json.dumps({"goal": "x" * 1000}), limit=50)), 50)
        self.assertIsNone(pick_text("not json"))


if __name__ == "__main__":
    unittest.main()


class MachineTest(unittest.TestCase):
    """Health of the machine Hermes runs on (collector/machine.py)."""

    def test_two_samples_give_rates_and_history(self):
        import time
        from machine import Machine
        m = Machine()
        first = m.sample()
        self.assertIsNone(first["cpu"]["percent"])  # nothing to compare against yet
        time.sleep(0.3)
        second = m.sample()
        self.assertIsNotNone(second["cpu"]["percent"])
        self.assertTrue(0 <= second["cpu"]["percent"] <= 100)
        self.assertGreater(second["mem"]["total"], 0)
        self.assertEqual(len(second["net"]), 2)
        self.assertEqual(len(second["history"]), 2)
        json.dumps(second)  # JSON-safe

    def test_history_keeps_fifteen_minutes(self):
        from machine import Machine, HISTORY_S
        m = Machine()
        for t in range(0, HISTORY_S + 600, 60):
            out = m.sample(now=1_700_000_000 + t)
        self.assertLessEqual(out["history"][-1][0] - out["history"][0][0], HISTORY_S)

    def test_container_cgroup(self):
        from machine import cgroup_stats, container_id
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cg = os.path.join(tmp.name, "system.slice", "docker-3f2a9c1b7d4e0011223344556677889900aabbccddeeff.scope")
        os.makedirs(cg)
        for name, text in (("cpu.stat", "usage_usec 5000000\nuser_usec 4000000\n"),
                           ("memory.current", "812000000\n"), ("memory.max", "max\n"),
                           ("memory.events", "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n"),
                           ("pids.current", "23\n")):
            with open(os.path.join(cg, name), "w") as f:
                f.write(text)
        self.assertEqual(container_id(cg), "3f2a9c1b7d4e")
        self.assertEqual(cgroup_stats(cg), {"cpu_usec": 5000000, "mem": 812000000, "mem_limit": None,
                                            "oom_kills": 1, "pids": 23})
        self.assertIsNone(container_id("/sys/fs/cgroup/user.slice/user-1000.slice"))

    def test_machine_reported_without_hermes_database(self):
        c = Collector(db_path="/nonexistent/state.db")
        r = c.report()
        self.assertFalse(r["db"]["ok"])
        self.assertIn("cpu", r["machine"])
        self.assertIn("_ms", r["machine"])
