"""Traffic pauses must neither abort native acceptance nor hide real failures."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services.resource_transfer_protocol import TransferError
from tests.native_traffic_retry import low_traffic_step


class NativeTrafficRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_verified_pause_waits_and_keeps_the_same_step(self):
        step = AsyncMock(side_effect=[
            TransferError('migration_transfer_waiting_low_traffic'),
            TransferError('migration_transfer_waiting_low_traffic'),
            {'source_verified': True},
        ])
        keepalive = AsyncMock()
        with patch('tests.native_traffic_retry.asyncio.sleep', new_callable=AsyncMock) as sleep:
            result = await low_traffic_step(step, keepalive)
        self.assertEqual(result, {'source_verified': True})
        self.assertEqual(step.await_count, 3)
        keepalive.assert_awaited_once()
        self.assertEqual(sleep.await_count, 2)

    async def test_other_protocol_and_transport_failures_are_not_retried(self):
        for error in (TransferError('migration_transfer_identity_invalid'), OSError('disconnected')):
            with self.subTest(error=type(error).__name__):
                step = AsyncMock(side_effect=error)
                keepalive = AsyncMock()
                with self.assertRaises(type(error)) as caught:
                    await low_traffic_step(step, keepalive)
                self.assertIs(caught.exception, error)
                step.assert_awaited_once()
                keepalive.assert_not_awaited()

    async def test_heartbeat_or_lease_failure_is_not_swallowed(self):
        step = AsyncMock(side_effect=TransferError('migration_transfer_waiting_low_traffic'))
        error = RuntimeError('fixture lease lost')
        keepalive = AsyncMock(side_effect=error)
        with self.assertRaises(RuntimeError) as caught:
            await low_traffic_step(step, keepalive)
        self.assertIs(caught.exception, error)
        step.assert_awaited_once()

    async def test_continuous_high_traffic_remains_a_bounded_failure(self):
        step = AsyncMock(side_effect=TransferError('migration_transfer_waiting_low_traffic'))
        keepalive = AsyncMock()
        with self.assertRaises(TimeoutError):
            await low_traffic_step(step, keepalive, timeout=0.02)
        step.assert_awaited_once()
        keepalive.assert_awaited_once()

    async def test_cancellation_stops_the_fixture_without_retry(self):
        step = AsyncMock(side_effect=asyncio.CancelledError)
        keepalive = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await low_traffic_step(step, keepalive)
        keepalive.assert_not_awaited()
