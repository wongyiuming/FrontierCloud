from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram
from starlette.types import ASGIApp, Receive, Scope, Send


HTTP_REQUESTS = Counter(
    "frontiercloud_http_requests_total",
    "HTTP requests handled by FrontierCloud",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "frontiercloud_http_request_duration_seconds",
    "FrontierCloud HTTP request duration",
    ("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
HTTP_IN_PROGRESS = Gauge(
    "frontiercloud_http_requests_in_progress",
    "FrontierCloud HTTP requests currently in progress",
    ("method",),
)
HTTP_EXCEPTIONS = Counter(
    "frontiercloud_http_exceptions_total",
    "Unhandled FrontierCloud HTTP exceptions",
    ("method", "route"),
)


def route_label(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class PrometheusMetricsMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "UNKNOWN"))[:16]
        status_code = 500
        started = time.perf_counter()
        HTTP_IN_PROGRESS.labels(method=method).inc()

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            HTTP_EXCEPTIONS.labels(method=method, route=route_label(scope)).inc()
            raise
        finally:
            route = route_label(scope)
            HTTP_IN_PROGRESS.labels(method=method).dec()
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()
            HTTP_DURATION.labels(method=method, route=route).observe(time.perf_counter() - started)
