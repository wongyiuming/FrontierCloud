import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

import main


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_dependencies_return_unavailable_and_cancel_queries(self):
        async def hang(*_args):
            await asyncio.Event().wait()
        connection = AsyncMock()
        connection.execute.side_effect = hang
        context = AsyncMock()
        context.__aenter__.return_value = connection
        with (
            patch("app.services.health.redis_client.ping", AsyncMock(side_effect=hang)),
            patch("app.services.health.engine", SimpleNamespace(connect=lambda: context)),
            patch("app.services.health.DEPENDENCY_TIMEOUT_SECONDS", 0.02),
        ):
            response = await asyncio.wait_for(main.health_ready(), 0.5)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["checks"], {"redis": "unavailable", "mysql": "unavailable"})
        context.__aexit__.assert_awaited_once()

    async def test_attacker_defined_http_methods_share_one_metrics_label(self):
        from app.core import metrics
        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 405, "headers": []})
        async def send(_message):
            pass
        requests = MagicMock()
        with (
            patch.object(metrics, "HTTP_REQUESTS", requests),
            patch.object(metrics, "HTTP_DURATION", MagicMock()),
            patch.object(metrics, "HTTP_IN_PROGRESS", MagicMock()),
        ):
            for method in ("ATTACK_ONE", "ATTACK_TWO"):
                await metrics.MetricsMiddleware(app)({"type": "http", "method": method}, AsyncMock(), send)
        self.assertEqual([call.kwargs["method"] for call in requests.labels.call_args_list], ["OTHER", "OTHER"])

    async def test_metrics_are_consumer_neutral_and_require_bearer_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "metrics_token"
            token_file.write_text("test-metrics-secret\n", encoding="utf-8")
            with patch("app.core.config.METRICS_TOKEN_FILE", token_file):
                with self.assertRaises(HTTPException) as missing:
                    await main.metrics(None)
                self.assertEqual(missing.exception.status_code, 404)
                response = await main.metrics("Bearer test-metrics-secret")
        body = bytes(response.body).decode("utf-8")
        self.assertIn("frontiercloud_http_requests_total", body)
        self.assertIn("frontiercloud_dependency_ready", body)

    async def test_readiness_checks_mysql_and_redis(self):
        connection = AsyncMock()
        connection.execute = AsyncMock()
        context = AsyncMock()
        context.__aenter__.return_value = connection
        with (
            patch("app.services.health.redis_client.ping", AsyncMock(return_value=True)),
            patch("app.services.health.engine", SimpleNamespace(connect=lambda: context)),
        ):
            response = await main.health_ready()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body)["checks"], {"redis": "ready", "mysql": "ready"})

    def test_no_consumer_specific_application_routes_exist(self):
        paths = {route.path for route in main.app.routes if hasattr(route, "path")}
        self.assertIn("/metrics", paths)
        self.assertIn("/health/live", paths)
        self.assertIn("/health/ready", paths)
        self.assertNotIn("/internal/report/security", paths)
        self.assertNotIn("/internal/report/access-log", paths)


if __name__ == "__main__":
    unittest.main()
