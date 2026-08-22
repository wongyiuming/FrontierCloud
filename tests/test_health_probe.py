import unittest
from unittest.mock import AsyncMock

from app.services import health_probe


class HealthProbeCounterTests(unittest.IsolatedAsyncioTestCase):
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
