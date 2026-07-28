#!/usr/bin/env python3
"""HTTP contract for the read-only outcome funnel."""

import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.request import urlopen

_EVENTS_TEMP_DIR = tempfile.TemporaryDirectory()
_ORIGINAL_EVENTS_DB = os.environ.get("CWC_EVENTS_DB")
os.environ["CWC_EVENTS_DB"] = str(Path(_EVENTS_TEMP_DIR.name) / "events.db")

import outcome_traces  # noqa: E402
from webhook_receiver import IssueWebhookHandler  # noqa: E402


class OutcomeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_path = outcome_traces.DEFAULT_TRACE_PATH
        outcome_traces.DEFAULT_TRACE_PATH = (
            Path(__file__).resolve().parent / "fixtures" / "outcome_traces.json"
        )
        cls.server = HTTPServer(("127.0.0.1", 0), IssueWebhookHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        outcome_traces.DEFAULT_TRACE_PATH = cls.original_path
        if _ORIGINAL_EVENTS_DB is None:
            os.environ.pop("CWC_EVENTS_DB", None)
        else:
            os.environ["CWC_EVENTS_DB"] = _ORIGINAL_EVENTS_DB
        _EVENTS_TEMP_DIR.cleanup()

    def get_json(self, path):
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_endpoint_is_read_only_and_filterable(self):
        status, payload = self.get_json("/api/outcome-funnel?client=zenna")
        self.assertEqual(status, 200)
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["acceptance_inference"], "forbidden")
        self.assertEqual(len(payload["traces"]), 1)
        self.assertEqual(payload["traces"][0]["client_slug"], "zenna")
        self.assertEqual(payload["traces"][0]["acceptance"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
