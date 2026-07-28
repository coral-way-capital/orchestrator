#!/usr/bin/env python3
"""HTTP behavior tests for Mission Control portfolio endpoints."""

import json
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import portfolio
from test_portfolio import manifest
from webhook_receiver import IssueWebhookHandler


class PortfolioHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.manifest_path = Path(cls.tempdir.name) / "projects.json"
        cls.manifest_path.write_text(json.dumps(manifest()), encoding="utf-8")
        cls.original_manifest_path = portfolio.DEFAULT_MANIFEST_PATH
        portfolio.DEFAULT_MANIFEST_PATH = cls.manifest_path
        cls.server = HTTPServer(("127.0.0.1", 0), IssueWebhookHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        portfolio.DEFAULT_MANIFEST_PATH = cls.original_manifest_path
        cls.tempdir.cleanup()

    def get_json(self, path):
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_portfolio_endpoint_returns_ranked_payload(self):
        status, payload = self.get_json("/api/portfolio")
        self.assertEqual(status, 200)
        self.assertEqual(payload["summary"]["total_projects"], 1)
        self.assertEqual(payload["projects"][0]["id"], "alpha")

    def test_project_endpoint_returns_one_project(self):
        status, payload = self.get_json("/api/portfolio/alpha")
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], "alpha")
        self.assertIn("score_breakdown", payload)

    def test_brief_endpoint_returns_inspectable_context(self):
        status, payload = self.get_json("/api/portfolio/alpha/brief")
        self.assertEqual(status, 200)
        self.assertEqual(payload["project_id"], "alpha")
        self.assertIn("Finish gate", payload["brief"])

    def test_malformed_manifest_returns_safe_warning(self):
        original = self.manifest_path.read_text(encoding="utf-8")
        bad_manifest = manifest()
        bad_manifest["projects"][0]["evidence"] = None
        self.manifest_path.write_text(json.dumps(bad_manifest), encoding="utf-8")
        try:
            with self.assertRaises(HTTPError) as error:
                self.get_json("/api/portfolio")
            self.assertEqual(error.exception.code, 503)
            payload = json.loads(error.exception.read().decode("utf-8"))
            self.assertEqual(payload["error"], "portfolio unavailable")
            self.assertEqual(payload["projects"], [])
        finally:
            self.manifest_path.write_text(original, encoding="utf-8")

    def test_unknown_project_returns_not_found(self):
        with self.assertRaises(HTTPError) as error:
            self.get_json("/api/portfolio/does-not-exist")
        self.assertEqual(error.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
