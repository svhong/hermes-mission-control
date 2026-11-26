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


class HelpersTest(unittest.TestCase):
    def test_parse_list(self):
        self.assertEqual(parse_list('["a", "b", "a"]'), ["a", "b"])
        self.assertEqual(parse_list("x, y"), ["x", "y"])
        self.assertEqual(parse_list(None), [])

    def test_pick_text_truncates_and_ignores_other_keys(self):
        self.assertEqual(pick_text('{"goal": "short"}'), "short")
        self.assertEqual(pick_text('{"task": {"goal": "nested"}}'), "nested")
        self.assertIsNone(pick_text('{"context": "secret"}'))
        self.assertEqual(len(pick_text(json.dumps({"goal": "x" * 1000}), limit=50)), 50)
        self.assertIsNone(pick_text("not json"))


if __name__ == "__main__":
    unittest.main()
