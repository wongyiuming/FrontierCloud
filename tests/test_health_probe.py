import unittest
import json
from unittest.mock import AsyncMock, patch

from app.services import health, health_probe


class _Connection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return None


class _Engine:
    def connect(self):
        return _Connection()


class HealthProbeCounterTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_contract_does_not_leak_database_backend(self):
        redis = AsyncMock()
        with patch.object(health, "engine", _Engine()), patch.object(health, "redis_client", redis):
            response = await health.readiness_response()
        self.assertEqual(response.status_code, 200)
        value = json.loads(response.body)
        self.assertEqual(value, {
            "status": "ready",
            "checks": {"database": "ready", "redis": "ready"},
        })
        self.assertNotIn("mysql", value["checks"])

    async def test_success_and_failure_use_a_bounded_120_minute_ring(self):
        client = AsyncMock()
        timestamp = 10_000 * 60

        await health_probe.record_health_result(True, timestamp=timestamp, client=client)
        await health_probe.record_health_result(False, timestamp=timestamp + 60, client=client)

        first = client.eval.await_args_list[0].args
        second = client.eval.await_args_list[1].args
        self.assertEqual(first[2], health_probe.ROLLING_KEY)
        self.assertEqual(first[3], 10_000 % 120)
        self.assertEqual(first[5], "success")
        self.assertEqual(second[3], 10_001 % 120)
        self.assertEqual(second[5], "failure")
        self.assertEqual(first[6], 7_200)
        self.assertIn("HINCRBY", health_probe.ROLLING_COUNTER_SCRIPT)
        self.assertIn("HSET", health_probe.ROLLING_COUNTER_SCRIPT)
        self.assertIn("EXPIRE", health_probe.ROLLING_COUNTER_SCRIPT)

    async def test_redis_accounting_failure_does_not_change_probe_result(self):
        client = AsyncMock()
        client.eval.side_effect = RuntimeError("redis unavailable")
        with self.assertRaises(RuntimeError):
            await health_probe.record_health_result(True, client=client)


if __name__ == "__main__":
    unittest.main()
