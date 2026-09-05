import asyncio
import threading
import weakref


class LoopLocalAsyncLock:
    """Provide one asyncio lock per running event loop."""

    def __init__(self) -> None:
        self._locks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, asyncio.Lock
        ] = weakref.WeakKeyDictionary()
        self._mapping_lock = threading.Lock()

    def _current(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        with self._mapping_lock:
            lock = self._locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[loop] = lock
            return lock

    async def __aenter__(self) -> "LoopLocalAsyncLock":
        await self._current().acquire()
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self._current().release()
