import inspect
import unittest
from unittest.mock import AsyncMock, patch

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

    async def test_weekly_security_report_uses_the_same_internal_secret(self):
        summary = {"unique_ip_count": 1, "addresses": [{"ip": "203.0.113.8"}]}
        with (
            patch.object(main.settings, "INTERNAL_METRICS_TOKEN", "test-internal-secret"),
            patch.object(main, "weekly_security_summary", AsyncMock(return_value=summary)) as reporter,
        ):
            with self.assertRaises(HTTPException) as missing:
                await main.internal_security_report(days=7, x_metrics_token=None)
            self.assertEqual(missing.exception.status_code, 404)
            self.assertEqual(
                await main.internal_security_report(days=7, x_metrics_token="test-internal-secret"),
                summary,
            )
            reporter.assert_awaited_once_with(7)


if __name__ == "__main__":
    unittest.main()
