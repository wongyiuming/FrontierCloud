import unittest
from unittest.mock import patch

import main


class RequestLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, scope, status=200, authenticated=False, session_ttl=None):
        async def app(request_scope, _receive, send):
            if authenticated:
                request_scope.update(admin_authenticated=True, admin_session_cookie="session-value")
                if session_ttl is not None:
                    request_scope["admin_session_ttl"] = session_ttl
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        with patch.object(main.logger, "info") as logged:
            await main.RealIPLogMiddleware(app)(scope, receive, send)
        return messages, logged

    async def test_quiet_success_is_suppressed_but_authentication_failure_is_logged(self):
        scope = {"type": "http", "method": "GET", "path": "/metrics", "headers": [], "client": ("127.0.0.1", 1)}
        _messages, success = await self._run(dict(scope), 200)
        _messages, failure = await self._run(dict(scope), 404)
        success.assert_not_called()
        self.assertEqual(failure.call_args.kwargs["extra"]["context"]["status"], 404)

    async def test_untrusted_request_id_and_invalid_trace_are_replaced(self):
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [
            (b"x-request-id", b"forged"), (b"traceparent", b"00-" + b"0" * 32 + b"-" + b"1" * 16 + b"-01")
        ], "client": ("203.0.113.8", 1)}
        await self._run(scope)
        self.assertNotEqual(scope["request_id"], "forged")
        self.assertEqual(len(scope["trace_id"]), 32)
        self.assertNotEqual(scope["trace_id"], "0" * 32)

    async def test_trusted_request_id_and_valid_trace_are_preserved(self):
        trace = "a" * 32
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [
            (b"x-real-ip", b"203.0.113.8"), (b"x-request-id", b"trusted-request"),
            (b"traceparent", ("00-" + trace + "-" + "b" * 16 + "-01").encode())
        ], "client": ("172.19.0.4", 1)}
        await self._run(scope)
        self.assertEqual(scope["request_id"], "trusted-request")
        self.assertEqual(scope["trace_id"], trace)

    async def test_authenticated_response_refreshes_both_idle_cookies(self):
        csrf_name = main.settings.ADMIN_CSRF_COOKIE_NAME
        scope = {"type": "http", "method": "GET", "path": "/admin/status", "headers": [
            (b"cookie", (csrf_name + "=csrf-value").encode())
        ], "client": ("127.0.0.1", 1)}
        messages, _logged = await self._run(scope, authenticated=True)
        cookies = [value.decode() for name, value in messages[0]["headers"] if name == b"set-cookie"]
        self.assertEqual(len(cookies), 2)
        self.assertTrue(all(f"Max-Age={main.settings.ADMIN_SESSION_TTL}" in value for value in cookies))
        self.assertTrue(any("HttpOnly" in value and main.settings.ADMIN_COOKIE_NAME in value for value in cookies))

    async def test_temporary_admin_response_refreshes_its_shorter_idle_window(self):
        csrf_name = main.settings.ADMIN_CSRF_COOKIE_NAME
        scope = {"type": "http", "method": "GET", "path": "/admin/status", "headers": [
            (b"cookie", (csrf_name + "=csrf-value").encode())
        ], "client": ("127.0.0.1", 1)}
        messages, _logged = await self._run(scope, authenticated=True, session_ttl=900)
        cookies = [value.decode() for name, value in messages[0]["headers"] if name == b"set-cookie"]
        self.assertTrue(all("Max-Age=900" in value for value in cookies))

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
