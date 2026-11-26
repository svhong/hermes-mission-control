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


if __name__ == "__main__":
    unittest.main()
