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
        self.assertEqual([t["id"] for t in r["tabs"]], ["overview", "agents"])
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
