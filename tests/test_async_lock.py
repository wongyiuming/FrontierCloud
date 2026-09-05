import asyncio
import unittest

from app.core.async_lock import LoopLocalAsyncLock


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


if __name__ == "__main__":
    unittest.main()
