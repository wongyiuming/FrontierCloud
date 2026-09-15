import asyncio
import threading
import weakref
from contextlib import asynccontextmanager


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


class _ReadWriteState:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0


class LoopLocalAsyncRWLock:
    """Share stable media reads; give queued filesystem writers precedence."""

    def __init__(self):
        self._states = weakref.WeakKeyDictionary()
        self._mapping_lock = threading.Lock()

    def _current(self):
        loop = asyncio.get_running_loop()
        with self._mapping_lock:
            state = self._states.get(loop)
            if state is None:
                state = self._states[loop] = _ReadWriteState()
            return state

    async def __aenter__(self):
        state = self._current()
        async with state.condition:
            state.waiting_writers += 1
            try:
                await state.condition.wait_for(lambda: not state.writer and state.readers == 0)
                state.writer = True
            finally:
                state.waiting_writers -= 1
                state.condition.notify_all()
        return self

    async def _release(self, state, *, writer):
        async with state.condition:
            if writer:
                state.writer = False
            else:
                state.readers -= 1
            state.condition.notify_all()

    async def _finish_release(self, state, *, writer):
        task = asyncio.create_task(self._release(state, writer=writer))
        cancellation = None
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError as exc:
                cancellation = exc
        if cancellation is not None:
            raise cancellation

    async def __aexit__(self, _exc_type, _exc, _traceback):
        await self._finish_release(self._current(), writer=True)

    @asynccontextmanager
    async def shared(self):
        state = self._current()
        async with state.condition:
            await state.condition.wait_for(lambda: not state.writer and state.waiting_writers == 0)
            state.readers += 1
        try:
            yield self
        finally:
            await self._finish_release(state, writer=False)
