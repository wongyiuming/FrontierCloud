import asyncio
import unittest

from app.core.async_lock import LoopLocalAsyncLock, LoopLocalAsyncRWLock


class LoopLocalAsyncLockTests(unittest.TestCase):
    def test_same_lock_can_contend_on_successive_event_loops(self):
        lock = LoopLocalAsyncLock()

        async def contend() -> list[str]:
            order = []
            first_entered = asyncio.Event()
            release_first = asyncio.Event()

            async def first() -> None:
                async with lock:
                    order.append("first")
                    first_entered.set()
                    await release_first.wait()

            async def second() -> None:
                await first_entered.wait()
                async with lock:
                    order.append("second")

            first_task = asyncio.create_task(first())
            second_task = asyncio.create_task(second())
            await first_entered.wait()
            await asyncio.sleep(0)
            release_first.set()
            await asyncio.gather(first_task, second_task)
            return order

        self.assertEqual(asyncio.run(contend()), ["first", "second"])
        self.assertEqual(asyncio.run(contend()), ["first", "second"])

    def test_readers_overlap_and_writer_waits_for_both(self):
        lock = LoopLocalAsyncRWLock()

        async def exercise():
            entered = 0
            both_reading = asyncio.Event()
            release = asyncio.Event()
            order = []

            async def reader(label):
                nonlocal entered
                async with lock.shared():
                    entered += 1
                    order.append(label)
                    if entered == 2:
                        both_reading.set()
                    await release.wait()

            async def writer():
                await both_reading.wait()
                async with lock:
                    order.append("writer")

            readers = [asyncio.create_task(reader(name)) for name in ("r1", "r2")]
            writing = asyncio.create_task(writer())
            await asyncio.wait_for(both_reading.wait(), 1)
            await asyncio.sleep(0)
            self.assertNotIn("writer", order)
            release.set()
            await asyncio.gather(*readers, writing)
            return order

        self.assertEqual(asyncio.run(exercise()), ["r1", "r2", "writer"])

    def test_cancelled_waiting_writer_unblocks_new_readers(self):
        lock = LoopLocalAsyncRWLock()

        async def exercise():
            reader_entered = asyncio.Event()
            release_reader = asyncio.Event()

            async def first_reader():
                async with lock.shared():
                    reader_entered.set()
                    await release_reader.wait()

            first = asyncio.create_task(first_reader())
            await reader_entered.wait()
            writer = asyncio.create_task(lock.__aenter__())
            await asyncio.sleep(0)
            writer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await writer
            second_entered = asyncio.Event()

            async def second_reader():
                async with lock.shared():
                    second_entered.set()

            second = asyncio.create_task(second_reader())
            await asyncio.wait_for(second_entered.wait(), 1)
            release_reader.set()
            await asyncio.gather(first, second)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
