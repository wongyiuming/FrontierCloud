"""Source contracts for the federation backup data path and retired worker UI."""
from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BackupProxyContractTests(unittest.TestCase):
    def test_encoded_backup_chunk_exceeds_default_64k_but_fits_backup_limit(self):
        body = json.dumps({
            "generation": 1,
            "chunk_index": 0,
            "chunk": base64.b64encode(b"x" * (192 * 1024)).decode("ascii"),
        }, separators=(",", ":")).encode("utf-8")
        self.assertGreater(len(body), 64 * 1024)
        self.assertLess(len(body), 512 * 1024)

    def test_nginx_has_bounded_backup_location(self):
        nginx = (ROOT / "nginx/nginx.conf").read_text(encoding="utf-8")
        match = re.search(
            r"location \^~ /internal/v1/backup/ \{(?P<body>.*?)\n        \}",
            nginx,
            re.S,
        )
        self.assertIsNotNone(match)
        block = match.group("body")
        self.assertIn("client_max_body_size 512k;", block)
        self.assertIn("proxy_request_buffering off;", block)
        self.assertIn("proxy_read_timeout 65s;", block)
        self.assertIn("client_max_body_size 64k;", nginx)

    def test_backup_requests_get_longer_control_timeout(self):
        transport = (ROOT / "app/services/federation/transport.py").read_text(encoding="utf-8")
        self.assertIn('path.startswith("/internal/v1/backup/")', transport)
        self.assertIn("httpx.Timeout(60, connect=8)", transport)


class ComputeRetirementContractTests(unittest.TestCase):
    def test_worker_is_not_a_product_or_runtime_feature(self):
        nodes = (ROOT / "static/js/nodes.js").read_text(encoding="utf-8")
        admin = (ROOT / "app/api/v1/admin_nodes.py").read_text(encoding="utf-8")
        runtime = (ROOT / "app/services/federation/runtime.py").read_text(encoding="utf-8")

        self.assertNotIn("Compute Worker", nodes)
        self.assertNotIn("worker_slots", nodes)
        self.assertNotIn("compute_enabled", nodes)
        settings = admin.split("class ResourceSettings", 1)[1].split("\n\ndef checked", 1)[0]
        self.assertNotIn("compute_enabled", settings)
        self.assertNotIn("worker_slots", settings)
        self.assertNotIn("fill_worker_slots", runtime)
        self.assertNotIn("execute_worker_job", runtime)
        self.assertNotIn("/internal/v1/jobs/lease", runtime)


if __name__ == "__main__":
    unittest.main()
