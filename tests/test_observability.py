import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import main


class ObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_are_consumer_neutral_and_require_bearer_token(self):
        with patch.object(main.settings, "METRICS_TOKEN", "test-metrics-secret"):
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
