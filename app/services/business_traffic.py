"""Bounded live HTTP I/O observations for background-work admission.

Counters contain no paths, headers, identities or media data. Public Nginx
sendfile transfers are observed through a local, read-only Unix socket; native
Storage transfers are observed at ASGI receive/send. Observations grant no
authorization and never override a job's durable manual-pause state.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

SOCKET = "/run/frontiercloud-traffic/counter.sock"
HIGH = 256 << 10
LOW = 64 << 10
WINDOW = 5
QUIET = 30
MAX_COUNTER = (1 << 63) - 1
FIELDS = frozenset({"format", "version", "epoch", "observed_at_ms",
                    "ingress_bytes_per_second", "egress_bytes_per_second",
                    "recent_bytes_per_second", "active_business_requests"})


@dataclass(frozen=True)
class GatewaySample:
    epoch: str
    observed_at_ms: int
    ingress: int
    egress: int
    recent: int
    active: int


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate traffic field")
        result[name] = value
    return result


def decode_gateway(raw: bytes, now: float) -> GatewaySample:
    if len(raw) > 4096:
        raise ValueError("Traffic observation is too large")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Invalid traffic JSON") from exc
    if not isinstance(value, dict) or value.keys() != FIELDS:
        raise ValueError("Invalid traffic fields")
    if (value["format"] != "frontiercloud-nginx-traffic" or
            type(value["version"]) is not int or value["version"] != 2 or
            not isinstance(value["epoch"], str) or
            re.fullmatch(r"[0-9]{1,12}-[0-9]{1,20}", value["epoch"]) is None):
        raise ValueError("Invalid traffic format")
    numbers = [value[name] for name in ("observed_at_ms", "ingress_bytes_per_second",
                                      "egress_bytes_per_second", "recent_bytes_per_second", "active_business_requests")]
    if (any(type(n) is not int or not 0 <= n <= MAX_COUNTER for n in numbers) or
            abs(numbers[0] - int(now * 1000)) > 2000):
        raise ValueError("Stale or invalid traffic observation")
    return GatewaySample(value["epoch"], *numbers)


class Monitor:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time, gateway_required: bool = False):
        self._clock, self._wall = monotonic, wall
        self._lock = threading.Lock()
        self._buckets = [[-1, 0, 0] for _ in range(50)]
        self._last = self._clock()
        self._quiet_since = self._last
        self._paused = True
        self._required = gateway_required
        self._gateway = None
        self._gateway_at = None
        self._unavailable = ""
        self._active = 0

    def entered(self):
        with self._lock:
            if self._active>=MAX_COUNTER:self._unavailable='active_request_overflow'
            else:self._active+=1

    def exited(self):
        with self._lock:
            if self._active<=0:self._unavailable='active_request_underflow'
            else:self._active-=1

    def require_gateway(self, required: bool):
        with self._lock:
            self._required = required
            self._gateway = self._gateway_at = None
            self._paused, self._quiet_since = True, None

    def unavailable(self, reason: str):
        with self._lock:
            self._unavailable = reason
            self._paused, self._quiet_since = True, None

    def record(self, ingress: int = 0, egress: int = 0):
        if any(type(n) is not int or n < 0 for n in (ingress, egress)):
            return
        with self._lock:
            now = self._clock()
            if now < self._last:
                self._buckets = [[-1, 0, 0] for _ in range(50)]
            tick = math.floor(now * 10)
            bucket = self._buckets[tick % 50]
            if bucket[0] != tick:
                bucket[:] = [tick, 0, 0]
            bucket[1] = min(MAX_COUNTER, bucket[1] + ingress)
            bucket[2] = min(MAX_COUNTER, bucket[2] + egress)
            self._view(now)

    def update_gateway(self, sample: GatewaySample | None):
        with self._lock:
            # A repeated/older timestamp cannot renew observation freshness.
            if (sample is not None and self._gateway is not None and
                    sample.epoch == self._gateway.epoch and
                    sample.observed_at_ms <= self._gateway.observed_at_ms):
                self._paused, self._quiet_since = True, None
                return
            if sample is None or self._gateway is None or sample.epoch != self._gateway.epoch:
                self._paused, self._quiet_since = True, None
            self._gateway = sample
            self._gateway_at = self._clock() if sample is not None else None
            self._view(self._clock())

    def snapshot(self) -> dict:
        with self._lock:
            return self._view(self._clock())

    def _view(self, now: float) -> dict:
        tick = math.floor(now * 10)
        incoming = outgoing = recent = 0
        for stamp, received, sent in self._buckets:
            age = tick - stamp
            if 0 <= age < 50:
                incoming += received
                outgoing += sent
                if age < 10:
                    recent += received + sent
        view = {"state": "unavailable", "can_run": False,
                "active_business_requests": None, "readers_clear": False,
                "observed_at_ms": int(self._wall() * 1000),
                "ingress_bytes_per_second": incoming // WINDOW,
                "egress_bytes_per_second": outgoing // WINDOW,
                "recent_bytes_per_second": recent,
                "high_bytes_per_second": HIGH, "low_bytes_per_second": LOW,
                "window_seconds": WINDOW, "low_interval_seconds": QUIET,
                "resume_after_seconds": 0,
                "metric_source": "nginx_http_io+python_asgi_body" if self._required else "python_asgi_body"}
        if now < self._last:
            self._buckets = [[-1, 0, 0] for _ in range(50)]
            self._paused, self._quiet_since = True, None
        self._last = now
        if self._required:
            gateway = self._gateway
            if (gateway is None or self._gateway_at is None or
                    not 0 <= now - self._gateway_at <= 2 or
                    abs(view["observed_at_ms"] - gateway.observed_at_ms) > 2000):
                self._paused, self._quiet_since = True, None
                view["reason"] = "edge_traffic_unavailable"
                return view
            # Proxy bodies appear at both layers; do not sum their duplicates.
            for name, number in (("ingress_bytes_per_second", gateway.ingress),
                                 ("egress_bytes_per_second", gateway.egress),
                                 ("recent_bytes_per_second", gateway.recent)):
                view[name] = max(view[name], number)
        if self._unavailable:
            self._paused, self._quiet_since = True, None
            view["reason"] = self._unavailable
            return view
        active=max(self._active,self._gateway.active) if self._required else self._active
        view.update(active_business_requests=active,readers_clear=active==0)
        rate = max(view["ingress_bytes_per_second"] + view["egress_bytes_per_second"],
                   view["recent_bytes_per_second"])
        if rate >= HIGH:
            self._paused = True
        if self._paused:
            view["state"] = "cooldown"
            if rate > LOW:
                self._quiet_since = None
                view["state"] = "high_traffic"
            else:
                if self._quiet_since is None:
                    self._quiet_since = now
                remaining = QUIET - (now - self._quiet_since)
                if remaining <= 0:
                    self._paused = False
                else:
                    view["resume_after_seconds"] = math.ceil(remaining)
        if not self._paused:
            view.update(state="low_traffic", can_run=True, resume_after_seconds=0)
        return view

    async def wait(self):
        while not self.snapshot()["can_run"]:
            await asyncio.sleep(1)

    async def watch_gateway(self, socket: str = SOCKET):
        self.require_gateway(True)
        transport = httpx.AsyncHTTPTransport(uds=socket, retries=0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
        try:
            async with httpx.AsyncClient(transport=transport, trust_env=False,
                    follow_redirects=False, timeout=1,
                    limits=httpx.Limits(max_connections=1, max_keepalive_connections=1)) as client:
                while True:
                    sample = None
                    try:
                        async with asyncio.timeout(1):
                            async with client.stream("GET", "http://gateway/traffic",
                                    headers={"Accept-Encoding": "identity"}) as response:
                                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                                    raise ValueError("Invalid traffic response")
                                raw = bytearray()
                                async for chunk in response.aiter_raw(chunk_size=4097):
                                    raw.extend(chunk)
                                    if len(raw) > 4096:
                                        raise ValueError("Traffic response is too large")
                                sample = decode_gateway(bytes(raw), self._wall())
                    except (httpx.HTTPError, OSError, ValueError, TimeoutError):
                        pass
                    self.update_gateway(sample)
                    await asyncio.sleep(1)
        finally:
            self.update_gateway(None)


def is_business(method: str, path: str) -> bool:
    if path.startswith(("/health", "/metrics")):
        return False
    if method == "GET" and path.startswith("/api/v1/media/admin/nodes"):
        return False
    if not path.startswith("/internal/v1/"):
        return True
    if path.startswith("/internal/v1/media/"):
        return method in {"GET", "HEAD"}
    if path.startswith("/internal/v1/storage/"):
        return method == "PUT"
    if path.startswith("/internal/v1/recordings/"):
        return method in {"GET", "PUT"} and not path.endswith("/stat")
    return False


class BusinessTrafficMiddleware:
    def __init__(self, app, monitor: Monitor):
        self.app, self.monitor = app, monitor

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_business(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return

        async def received():
            message = await receive()
            if message["type"] == "http.request":
                self.monitor.record(ingress=len(message.get("body", b"")))
            return message

        async def sent(message):
            await send(message)
            if message["type"] == "http.response.body":
                self.monitor.record(egress=len(message.get("body", b"")))
            elif message["type"] == "http.response.pathsend":
                self.monitor.unavailable("unobserved_asgi_pathsend")

        self.monitor.entered()
        try:await self.app(scope, received, sent)
        finally:self.monitor.exited()


monitor = Monitor()
