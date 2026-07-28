#!/usr/bin/env python3
"""HTTP contract tests for the read-only repository-context audit endpoint."""

import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import context_audit_reports
from test_context_audit_reports import write_report_root

_EVENTS_TEMP_DIR = tempfile.TemporaryDirectory()
_ORIGINAL_EVENTS_DB = os.environ.get("CWC_EVENTS_DB")
os.environ["CWC_EVENTS_DB"] = str(Path(_EVENTS_TEMP_DIR.name) / "events.db")

from webhook_receiver import IssueWebhookHandler  # noqa: E402


class ContextAuditHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tempdir.name)
        write_report_root(cls.root)
        cls.original_root = context_audit_reports.DEFAULT_REPORT_ROOT
        context_audit_reports.DEFAULT_REPORT_ROOT = cls.root
        cls.server = HTTPServer(("127.0.0.1", 0), IssueWebhookHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        context_audit_reports.DEFAULT_REPORT_ROOT = cls.original_root
        cls.tempdir.cleanup()
        if _ORIGINAL_EVENTS_DB is None:
            os.environ.pop("CWC_EVENTS_DB", None)
        else:
            os.environ["CWC_EVENTS_DB"] = _ORIGINAL_EVENTS_DB
        # events is process-global once webhook_receiver is imported. Keep this
        # temporary directory alive through discovery so later HTTP suites see
        # the same isolated database; TemporaryDirectory cleans it at exit.

    def get_json(self, path="/api/context-audit"):
        with urlopen(self.base_url + path, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_endpoint_returns_redacted_canonical_report(self):
        status, payload = self.get_json()
        self.assertEqual(status, 200)
        self.assertEqual(payload["report_kind"], "delta")
        self.assertEqual(len(payload["repositories"]), 2)
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_endpoint_is_get_only(self):
        request = Request(
            self.base_url + "/api/context-audit",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=5)
        self.assertIn(error.exception.code, (403, 404))

    def test_unavailable_report_is_safe_503(self):
        original = context_audit_reports.DEFAULT_REPORT_ROOT
        context_audit_reports.DEFAULT_REPORT_ROOT = self.root / "missing"
        try:
            with self.assertRaises(HTTPError) as error:
                self.get_json()
            self.assertEqual(error.exception.code, 503)
            payload = json.loads(error.exception.read())
            self.assertFalse(payload["available"])
            self.assertNotIn(str(self.root), json.dumps(payload))
        finally:
            context_audit_reports.DEFAULT_REPORT_ROOT = original


if __name__ == "__main__":
    unittest.main()
