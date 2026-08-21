import inspect
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main


class ApplicationMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_metrics_require_the_internal_secret(self):
        with patch.object(main.settings, "INTERNAL_METRICS_TOKEN", "test-internal-secret"):
            with self.assertRaises(HTTPException) as missing:
                await main.internal_metrics(None)
            self.assertEqual(missing.exception.status_code, 404)

            response = await main.internal_metrics("test-internal-secret")

        body = bytes(response.body).decode("utf-8")
        self.assertIn("frontiercloud_http_requests_total", body)
        self.assertIn("frontiercloud_http_request_duration_seconds", body)
        self.assertIn("frontiercloud_http_exceptions_total", body)

    def test_metric_route_labels_use_templates_not_raw_request_paths(self):
        source = inspect.getsource(main.PrometheusMetricsMiddleware)
        self.assertIn("route_label(scope)", source)
        self.assertNotIn('scope.get("path"', source)


if __name__ == "__main__":
    unittest.main()
