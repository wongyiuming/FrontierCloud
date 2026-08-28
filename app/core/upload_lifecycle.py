import asyncio
from collections.abc import AsyncGenerator, AsyncIterator

import starlette.formparsers
import starlette.requests
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartParser

from app.core.config import settings


class ManagedMultiPartParser(MultiPartParser):
    """Bound request-body inactivity and close spooled files on every failure."""

    def __init__(self, *args, inactivity_timeout: int | float | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.inactivity_timeout = (
            settings.ADMIN_UPLOAD_INACTIVITY_TIMEOUT
            if inactivity_timeout is None
            else inactivity_timeout
        )

    async def _bounded_stream(self, stream: AsyncGenerator[bytes, None]) -> AsyncIterator[bytes]:
        iterator = stream.__aiter__()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        anext(iterator),
                        timeout=self.inactivity_timeout,
                    )
                except StopAsyncIteration:
                    return
                except TimeoutError as exc:
                    raise HTTPException(
                        status_code=408,
                        detail=(
                            "Upload connection was inactive for "
                            f"{self.inactivity_timeout:g} seconds"
                        ),
                    ) from exc
                yield chunk
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    def _close_spooled_files(self) -> None:
        for file in self._files_to_close_on_error:
            file.close()

    async def parse(self):
        original_stream = self.stream
        self.stream = self._bounded_stream(original_stream)
        try:
            return await super().parse()
        except BaseException:
            self._close_spooled_files()
            raise
        finally:
            self.stream = original_stream


def install_upload_lifecycle_guard() -> None:
    """Install the guarded parser used by Starlette Request.form()."""

    starlette.formparsers.MultiPartParser = ManagedMultiPartParser
    starlette.requests.MultiPartParser = ManagedMultiPartParser
