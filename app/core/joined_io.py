"""Join bounded file I/O before releasing its caller's resources.

This helper is for existing blocking filesystem operations, never CPU-heavy
work. Cancellation before execution prevents the operation from starting;
cancellation after execution starts waits for its actual completion, including
when shutdown also cancels the helper task.
"""
import asyncio
import threading


async def joined_io(operation, *args, on_cancel=None):
    loop = asyncio.get_running_loop()
    completed = loop.create_future()
    lock = threading.Lock()
    pending = [True]

    def finish(value, error):
        if not completed.done():
            completed.set_result((value, error))

    def work():
        with lock:
            if not pending[0]:
                return
            pending[0] = False
        value = error = None
        try:
            value = operation(*args)
        except BaseException as exc:
            error = exc
        finally:
            loop.call_soon_threadsafe(finish, value, error)

    def joined(task):
        error = asyncio.CancelledError() if task.cancelled() else task.exception()
        with lock:
            unused, pending[0] = pending[0], False
        if unused:
            finish(None, error or RuntimeError('File I/O did not start'))

    worker = asyncio.create_task(asyncio.to_thread(work), name='joined-file-io')
    worker.add_done_callback(joined)
    cancellation = None
    while True:
        try:
            value, error = await asyncio.shield(completed)
            break
        except asyncio.CancelledError as exc:
            cancellation = exc
    if cancellation is not None:
        if error is None and on_cancel is not None:
            await joined_io(on_cancel, value)
        raise cancellation
    if error is not None:
        raise error
    return value
