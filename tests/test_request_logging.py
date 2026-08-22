import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main


class RequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_and_monitoring_polls_are_silent(self):
        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        for path in main.QUIET_REQUEST_PATHS:
            scope = {
                "type": "http",
                "method": "GET",
                "path": path,
                "query_string": b"",
                "client": ("127.0.0.1", 31000),
                "verified_client_ip": "127.0.0.1",
            }
            output = io.StringIO()
            with redirect_stdout(output), patch.object(main, "append_admin_log") as append_log:
                await main.RealIPLogMiddleware(app)(scope, receive, send)
            self.assertEqual(output.getvalue(), "")
            append_log.assert_not_called()

    async def test_webrtc_observation_is_merged_into_one_request_line(self):
        async def app(scope, receive, send):
            scope["webrtc_observation"] = {
                "addresses": ["198.51.100.7", "2001:db8::7"],
                "matches_verified": False,
                "outcome": "ok",
            }
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/media/network-observation",
            "query_string": b"",
            "client": ("172.18.0.10", 31000),
            "verified_client_ip": "203.0.113.5",
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        output = io.StringIO()
        with redirect_stdout(output), patch.object(main, "append_admin_log") as append_log:
            await main.RealIPLogMiddleware(app)(scope, receive, send)

        lines = [line for line in output.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("[LOG]", lines[0])
        self.assertNotIn("[WEBRTC_IP]", lines[0])
        self.assertIn("[REQUEST] REAL_IP: 203.0.113.5", lines[0])
        self.assertIn("PROXY_IP: 172.18.0.10", lines[0])
        self.assertIn("WEBRTC_IP: 198.51.100.7,2001:db8::7", lines[0])
        self.assertIn("WEBRTC_MATCH: false", lines[0])
        append_log.assert_called_once_with(lines[0])


if __name__ == "__main__":
    unittest.main()
