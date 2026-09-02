import unittest
from unittest.mock import patch

import main


class RequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_log_contains_safe_structured_context(self):
        async def app(scope, _receive, send):
            scope["webrtc_observation"] = {"addresses": ["198.51.100.7"], "outcome": "ok"}
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        scope = {
            "type": "http", "method": "GET", "path": "/api/v1/media/",
            "query_string": b"", "headers": [], "client": ("172.18.0.2", 1234),
            "verified_client_ip": "203.0.113.5",
        }
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        with patch.object(main.logger, "info") as log:
            await main.RealIPLogMiddleware(app)(scope, receive, send)
        context = log.call_args.kwargs["extra"]["context"]
        self.assertEqual(context["client_ip"], "203.0.113.5")
        self.assertEqual(context["status"], 200)
        self.assertNotIn("proxy_ip", context)
        response_headers = dict(sent[0]["headers"])
        self.assertTrue(response_headers[b"x-request-id"])


if __name__ == "__main__":
    unittest.main()
