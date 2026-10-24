import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from app.services.business_traffic import (
    HIGH, LOW, BusinessTrafficMiddleware, GatewaySample, Monitor, decode_gateway, is_business,
)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def payload(clock, **changes):
    value = dict(format="frontiercloud-nginx-traffic", version=2, epoch="123-456",
                 observed_at_ms=int(clock() * 1000), ingress_bytes_per_second=0,
                 egress_bytes_per_second=0, recent_bytes_per_second=0, active_business_requests=0)
    value.update(changes)
    return json.dumps(value).encode()


class TrafficTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.monitor = Monitor(monotonic=self.clock, wall=self.clock)

    def test_initial_quiet_interval_then_burst_and_new_quiet_interval(self):
        self.assertFalse(self.monitor.snapshot()["can_run"])
        self.clock.now += 30
        self.assertTrue(self.monitor.snapshot()["can_run"])
        self.monitor.record(egress=HIGH)
        view = self.monitor.snapshot()
        self.assertFalse(view["can_run"])
        self.assertEqual(view["state"], "high_traffic")
        self.assertEqual(view["recent_bytes_per_second"], HIGH)
        self.clock.now += 5
        self.assertEqual(self.monitor.snapshot()["resume_after_seconds"], 30)
        self.clock.now += 29
        self.assertFalse(self.monitor.snapshot()["can_run"])
        self.clock.now += 1
        self.assertTrue(self.monitor.snapshot()["can_run"])

    def test_hysteresis_and_ingress_plus_egress(self):
        self.clock.now += 30
        self.monitor.snapshot()
        self.monitor.record(ingress=LOW + 1)
        self.assertTrue(self.monitor.snapshot()["can_run"])
        self.monitor.record(egress=HIGH)
        self.assertFalse(self.monitor.snapshot()["can_run"])
        self.clock.now += 1
        self.monitor.record(ingress=LOW + 1)
        self.assertEqual(self.monitor.snapshot()["state"], "high_traffic")

    def test_fixed_bucket_count_invalid_bytes_and_large_values(self):
        self.monitor.record(-1, 500)
        self.monitor.record(True, 500)
        self.assertEqual(self.monitor.snapshot()["recent_bytes_per_second"], 0)
        for _ in range(1000):
            self.monitor.record(1 << 100, 1 << 100)
            self.clock.now += .1
        self.assertEqual(len(self.monitor._buckets), 50)
        self.assertTrue(all(0 <= bucket[1] < 1 << 63 for bucket in self.monitor._buckets))

    def test_monotonic_rollback_restarts_quiet_window(self):
        self.clock.now += 30
        self.assertTrue(self.monitor.snapshot()["can_run"])
        self.clock.now -= 100
        self.assertFalse(self.monitor.snapshot()["can_run"])
        self.assertEqual(self.monitor.snapshot()["resume_after_seconds"], 30)

    def test_gateway_stale_missing_and_epoch_change_fail_closed(self):
        self.monitor.require_gateway(True)
        self.assertEqual(self.monitor.snapshot()["reason"], "edge_traffic_unavailable")
        for _ in range(31):
            self.monitor.update_gateway(decode_gateway(payload(self.clock), self.clock()))
            self.clock.now += 1
        self.assertTrue(self.monitor.snapshot()["can_run"])
        self.clock.now += 3
        self.assertFalse(self.monitor.snapshot()["can_run"])
        self.monitor.update_gateway(decode_gateway(payload(self.clock, epoch="222-333"), self.clock()))
        self.assertEqual(self.monitor.snapshot()["resume_after_seconds"], 30)
        self.monitor.update_gateway(None)
        self.assertEqual(self.monitor.snapshot()["state"], "unavailable")

    def test_replayed_sample_does_not_extend_freshness(self):
        self.monitor.require_gateway(True)
        sample = decode_gateway(payload(self.clock), self.clock())
        self.monitor.update_gateway(sample)
        self.clock.now += 1
        self.monitor.update_gateway(sample)
        self.clock.now += 1.1
        self.assertEqual(self.monitor.snapshot()["state"], "unavailable")

    def test_proxy_bytes_use_maximum_and_native_bytes_are_retained(self):
        self.monitor.require_gateway(True)
        self.monitor.record(500, 1000)
        self.monitor.update_gateway(GatewaySample("1-2", int(self.clock()*1000), 200, 150, 1200, 0))
        view = self.monitor.snapshot()
        self.assertEqual(view["ingress_bytes_per_second"], 200)
        self.assertEqual(view["egress_bytes_per_second"], 200)
        self.assertEqual(view["recent_bytes_per_second"], 1500)

    def test_malformed_samples_rejected(self):
        for changes in ({"version": True}, {"epoch": "other"}, {"unknown": 1},
                        {"version": 1}, {"active_business_requests": True}, {"active_business_requests": -1},
                        {"recent_bytes_per_second": -1}, {"observed_at_ms": 1},
                        {"ingress_bytes_per_second": 1.5}, {"egress_bytes_per_second": 1 << 64}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                decode_gateway(payload(self.clock, **changes), self.clock())
        raw = payload(self.clock)
        for invalid in (raw + b"{}", b"[]", raw[:-1] + b',"version":1}', b" " * 4097,
                        b'[' * 1500 + b']' * 1500):
            with self.assertRaises(ValueError):
                decode_gateway(invalid, self.clock())

    def test_low_rate_reader_is_not_confused_with_a_safe_cleanup_window(self):
        self.clock.now+=30
        self.monitor.entered()
        view=self.monitor.snapshot()
        self.assertTrue(view['can_run']);self.assertFalse(view['readers_clear'])
        self.assertEqual(view['active_business_requests'],1)
        self.monitor.exited();self.assertTrue(self.monitor.snapshot()['readers_clear'])

    def test_gateway_reader_and_stale_observation_never_become_zero(self):
        self.monitor.require_gateway(True)
        self.monitor.update_gateway(decode_gateway(payload(self.clock,active_business_requests=2),self.clock()))
        self.assertEqual(self.monitor.snapshot()['active_business_requests'],2)
        self.assertFalse(self.monitor.snapshot()['readers_clear'])
        self.clock.now+=3
        self.assertIsNone(self.monitor.snapshot()['active_business_requests'])
        self.assertFalse(self.monitor.snapshot()['readers_clear'])

    def test_reader_counter_underflow_requires_reconciliation(self):
        self.monitor.exited()
        self.assertFalse(self.monitor.snapshot()['readers_clear'])
        self.assertEqual(self.monitor.snapshot()['reason'],'active_request_underflow')

    def test_control_and_migration_bytes_are_not_business(self):
        for method, path in (("GET", "/health/ready"), ("GET", "/metrics"),
                             ("GET", "/api/v1/media/admin/nodes/1"),
                             ("POST", "/internal/v1/heartbeat"),
                             ("PUT", "/internal/v1/migration/data/1"),
                             ("POST", "/internal/v1/storage/1/stat")):
            self.assertFalse(is_business(method, path), path)
        for method, path in (("PUT", "/internal/v1/storage/1"),
                             ("GET", "/internal/v1/media/1"),
                             ("PUT", "/internal/v1/recordings/1"),
                             ("GET", "/static/logo.png"),
                             ("POST", "/api/v1/media/admin/upload/1")):
            self.assertTrue(is_business(method, path), path)


class TrafficAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_measures_chunks_before_request_completion(self):
        clock = Clock()
        monitor = Monitor(monotonic=clock, wall=clock)
        entered, release = asyncio.Event(), asyncio.Event()
        received = []

        async def app(scope, receive, send):
            self.assertEqual((await receive())["body"], b"input")
            await send({"type": "http.response.body", "body": b"output", "more_body": True})
            entered.set()
            await release.wait()

        async def receive():
            return {"type": "http.request", "body": b"input"}

        async def send(message):
            received.append(message)

        task = asyncio.create_task(BusinessTrafficMiddleware(app, monitor)(
            {"type": "http", "method": "PUT", "path": "/internal/v1/storage/1"}, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertFalse(task.done())
            self.assertEqual(monitor.snapshot()["recent_bytes_per_second"], 11)
            self.assertEqual(monitor.snapshot()['active_business_requests'],1)
            self.assertFalse(monitor.snapshot()['readers_clear'])
        finally:
            release.set()
            await task
        self.assertTrue(monitor.snapshot()['readers_clear'])

    async def test_control_bypass_and_send_backpressure(self):
        monitor = Monitor()
        started = asyncio.Event()

        async def app(scope, receive, send):
            await send({"type": "http.response.body", "body": b"x" * HIGH})

        async def send(message):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(BusinessTrafficMiddleware(app, monitor)(
            {"type": "http", "method": "GET", "path": "/api/v1/media/stream/1"}, None, send))
        await started.wait()
        self.assertEqual(monitor.snapshot()["recent_bytes_per_second"], 0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(monitor.snapshot()['readers_clear'])

        async def delivered(message):
            pass

        await BusinessTrafficMiddleware(app, monitor)(
            {"type": "http", "method": "GET", "path": "/health"}, None, delivered)
        self.assertEqual(monitor.snapshot()["recent_bytes_per_second"], 0)

    async def test_wait_is_cancellable_and_does_not_spin(self):
        task = asyncio.create_task(Monitor().wait())
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_real_unix_socket_polling_and_joined_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            socket = str(Path(directory)/"counter.sock")
            requests = []
            loop = asyncio.get_running_loop()
            accepted = asyncio.Event()

            async def connection(reader, writer):
                try:
                    requests.append(await reader.readuntil(b"\r\n\r\n"))
                    import time
                    raw = payload(time.time)
                    writer.write(b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: " +
                                 str(len(raw)).encode() + b"\r\n\r\n" + raw)
                    await writer.drain()
                finally:
                    writer.close()
                    await writer.wait_closed()
                    accepted.set()

            server = await asyncio.start_unix_server(connection, socket)
            monitor = Monitor()
            task = asyncio.create_task(monitor.watch_gateway(socket))
            try:
                await asyncio.wait_for(accepted.wait(), 2)
                deadline = loop.time() + 2
                while monitor.snapshot().get("reason") and loop.time() < deadline:
                    await asyncio.sleep(.01)
                self.assertEqual(monitor.snapshot()["state"], "cooldown")
                self.assertTrue(requests[0].startswith(b"GET /traffic HTTP/1.1\r\n"))
                self.assertNotIn(b"authorization:", requests[0].lower())
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                server.close()
                await server.wait_closed()
            self.assertEqual(monitor.snapshot()["state"], "unavailable")

    async def test_missing_socket_keeps_work_paused(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor()
            task = asyncio.create_task(monitor.watch_gateway(str(Path(directory)/"missing.sock")))
            try:
                await asyncio.sleep(.1)
                self.assertEqual(monitor.snapshot()["state"], "unavailable")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
