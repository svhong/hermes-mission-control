"""Run with: python3 -m unittest discover -s tests"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "hub"))

os.environ["HMC_TOKEN"] = "test-token"
import hub  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hub.TOKEN = "test-token"
        hub.PASSWORD_HASH = hub.hash_password("secret", rounds=1000)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self):
        code, body = self.call("/api/login", {"password": "secret"})
        self.assertEqual(code, 200)
        return json.loads(body)["token"]

    def test_report_needs_token(self):
        report = {"source": "hermes", "tabs": [{"id": "overview", "title": "Overview"}], "data": {}}
        self.assertEqual(self.call("/api/report", report)[0], 401)
        self.assertEqual(self.call("/api/report", report, {"X-HMC-Token": "test-token"})[0], 200)

    def test_bad_source_name_rejected(self):
        self.assertEqual(self.call("/api/report", {"source": "../x"}, {"X-HMC-Token": "test-token"})[0], 400)

    def test_state_needs_login(self):
        self.assertEqual(self.call("/api/state")[0], 401)
        self.assertEqual(self.call("/api/login", {"password": "wrong"})[0], 401)
        token = self.login()
        self.call("/api/report", {"source": "hermes", "tabs": [{"id": "overview", "title": "Overview"},
                                                               {"id": "no-renderer", "title": "X"}], "data": {}},
                  {"X-HMC-Token": "test-token"})
        code, body = self.call("/api/state", headers={"Authorization": "Bearer " + token})
        self.assertEqual(code, 200)
        state = json.loads(body)
        # Only tabs with a renderer in static/tabs are offered.
        self.assertEqual([t["id"] for t in state["tabs"]], ["overview"])

    def test_static_files_and_traversal(self):
        self.assertEqual(self.call("/")[0], 200)
        self.assertEqual(self.call("/static/app.js")[0], 200)
        self.assertEqual(self.call("/static/../hub.py")[0], 404)
        self.assertEqual(self.call("/static/%2e%2e/hub.py")[0], 404)


class LedgerTest(unittest.TestCase):
    def test_counts_growth_per_day_and_survives_restart(self):
        import tempfile
        import time
        lt = time.localtime()
        noon = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0, 0, 0, -1))
        yesterday = noon - 86400
        tomorrow = noon + 86400
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.json")
            led = hub.Ledger(path)
            # A Telegram chat from yesterday, first seen today with 1000 tokens: count from here.
            led.update("hermes", [["chat", yesterday, 1000]], now=noon)
            led.update("hermes", [["chat", yesterday, 1500], ["new", noon - 60, 300]], now=noon + 600)
            today = led.view("hermes")[hub.Ledger.day_key(noon)]
            self.assertEqual(today["tokens"], 500 + 300)   # new session counted from 0
            self.assertEqual(today["since"], noon)         # partial: chat's earlier use unknown
            led.save()
            led2 = hub.Ledger(path)                        # restart
            led2.update("hermes", [["chat", yesterday, 1700]], now=tomorrow)
            nxt = led2.view("hermes")[hub.Ledger.day_key(tomorrow)]
            self.assertEqual(nxt["tokens"], 200)           # baseline carried from previous day
            self.assertIsNone(nxt["since"])                # complete day

    def test_ignores_bad_rows(self):
        led = hub.Ledger()
        led.update("hermes", [["x"], None, ["ok", 0, "nope"], ["fine", 0, 5]])
        self.assertEqual(len(led.days["hermes"]), 1)


if __name__ == "__main__":
    unittest.main()
