"""Source contracts for the federation backup data path and retired worker UI."""
from __future__ import annotations

import base64
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def effective_routes(routes):
    """Flatten preserved include-router trees for route-surface assertions."""
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if callable(candidates):
            yield from effective_routes(candidates())
        else:
            yield route


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
        self.assertIn("self.backup_client", transport)
        self.assertIn("client = self.backup_client if is_backup else self.client", transport)

    def test_interrupted_generations_have_authenticated_abort_cleanup(self):
        control = (ROOT / "app/api/internal_backup_control.py").read_text(encoding="utf-8")
        registry = (ROOT / "app/api/internal_cluster_update.py").read_text(encoding="utf-8")
        runtime = (ROOT / "app/services/federation/runtime.py").read_text(encoding="utf-8")

        self.assertIn('router = APIRouter(prefix="/internal/v1/backup"', control)
        self.assertIn('@router.post("/abort")', control)
        self.assertIn("relation = await authenticated(request)", control)
        self.assertIn('relation.get("direction") != "upstream"', control)
        self.assertIn('state.node.get("role") != "Follower"', control)
        self.assertIn('state="failed"', control)
        self.assertIn("delete(s.business_backup_chunks)", control)
        self.assertIn("router.include_router(backup_control_router)", registry)
        self.assertGreaterEqual(runtime.count('"/internal/v1/backup/abort"'), 2)


class ComputeRetirementContractTests(unittest.TestCase):
    def test_worker_is_not_a_product_or_runtime_feature(self):
        nodes = (ROOT / "static/js/nodes.js").read_text(encoding="utf-8")
        admin_html = (ROOT / "static/media/admin.html").read_text(encoding="utf-8")
        admin = (ROOT / "app/api/v1/admin_nodes.py").read_text(encoding="utf-8")
        runtime = (ROOT / "app/services/federation/runtime.py").read_text(encoding="utf-8")
        observability = (ROOT / "app/services/node_observability.py").read_text(encoding="utf-8")

        self.assertNotIn("<th>Compute</th>", admin_html)
        self.assertNotIn("Compute Worker", nodes)
        self.assertNotIn("worker_slots", nodes)
        self.assertNotIn("compute_enabled", nodes)
        self.assertNotIn("storage-capacity-refresh.js", admin_html)
        self.assertIn("cell.colSpan = 4", nodes)
        settings = admin.split("class ResourceSettings", 1)[1].split("\n\ndef checked", 1)[0]
        self.assertNotIn("compute_enabled", settings)
        self.assertNotIn("worker_slots", settings)
        self.assertNotIn("fill_worker_slots", runtime)
        self.assertNotIn("execute_worker_job", runtime)
        self.assertNotIn("/internal/v1/jobs/lease", runtime)
        self.assertIn('resources["compute"] = {"enabled": False, "worker_slots": 0}', runtime)
        self.assertNotIn("worker_jobs", observability)
        self.assertNotIn("shared_queued", observability)

    def test_worker_control_routes_are_not_mounted(self):
        from main import app

        paths = {
            str(getattr(route, "path", ""))
            for route in effective_routes(app.routes)
        }
        self.assertFalse(
            any(path.startswith("/internal/v1/jobs/") for path in paths),
            sorted(path for path in paths if "/jobs/" in path),
        )
        self.assertIn("/internal/v1/identity", paths)
        self.assertIn("/internal/v1/heartbeat", paths)

    def test_retirement_filter_is_installed_before_internal_router_registration(self):
        retirement = (ROOT / "app/api/internal_worker_retirement.py").read_text(encoding="utf-8")
        registry = (ROOT / "app/api/internal_cluster_update.py").read_text(encoding="utf-8")

        self.assertIn('RETIRED_WORKER_PREFIX = "/internal/v1/jobs/"', retirement)
        self.assertIn("internal_nodes.router.routes[:]", retirement)
        self.assertIn("install_internal_worker_retirement()", registry)


if __name__ == "__main__":
    unittest.main()
