import asyncio
import unittest

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from app.core.upload_lifecycle import ManagedMultiPartParser


BOUNDARY = b"frontiercloud-test-boundary"
FILE_HEADER = (
    b"--" + BOUNDARY + b"\r\n"
    b'Content-Disposition: form-data; name="file"; filename="sample.wav"\r\n'
    b"Content-Type: audio/wav\r\n\r\n"
)


def multipart_headers() -> Headers:
    return Headers({
        "Content-Type": f"multipart/form-data; boundary={BOUNDARY.decode()}",
    })


class UploadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_form_stays_open_until_its_owner_closes_it(self):
        async def stream():
            yield FILE_HEADER + b"RIFF0000WAVEfmt "
            yield b"\r\n--" + BOUNDARY + b"--\r\n"

        parser = ManagedMultiPartParser(multipart_headers(), stream())
        form = await parser.parse()
        upload = form["file"]

        self.assertFalse(upload.file.closed)
        await form.close()
        self.assertTrue(upload.file.closed)

    async def test_client_disconnect_closes_the_spooled_upload(self):
        async def stream():
            yield FILE_HEADER + (b"x" * (1024 * 1024 + 1))
            raise ClientDisconnect()

        parser = ManagedMultiPartParser(multipart_headers(), stream())

        with self.assertRaises(ClientDisconnect):
            await parser.parse()

        self.assertEqual(len(parser._files_to_close_on_error), 1)
        self.assertTrue(parser._files_to_close_on_error[0].closed)

    async def test_task_cancellation_closes_the_spooled_upload(self):
        async def stream():
            yield FILE_HEADER + b"partial"
            raise asyncio.CancelledError()

        parser = ManagedMultiPartParser(multipart_headers(), stream())

        with self.assertRaises(asyncio.CancelledError):
            await parser.parse()

        self.assertEqual(len(parser._files_to_close_on_error), 1)
        self.assertTrue(parser._files_to_close_on_error[0].closed)

    async def test_inactivity_timeout_returns_408_and_closes_the_spooled_upload(self):
        never_resumes = asyncio.Event()

        async def stream():
            yield FILE_HEADER + b"partial"
            await never_resumes.wait()
            yield b"unreachable"

        parser = ManagedMultiPartParser(
            multipart_headers(),
            stream(),
            inactivity_timeout=0.01,
        )

        with self.assertRaises(HTTPException) as raised:
            await parser.parse()

        self.assertEqual(raised.exception.status_code, 408)
        self.assertEqual(len(parser._files_to_close_on_error), 1)
        self.assertTrue(parser._files_to_close_on_error[0].closed)

    async def test_parser_error_closes_the_spooled_upload(self):
        async def stream():
            yield FILE_HEADER + b"partial"
            raise OSError("simulated request-body failure")

        parser = ManagedMultiPartParser(multipart_headers(), stream())

        with self.assertRaises(OSError):
            await parser.parse()

        self.assertEqual(len(parser._files_to_close_on_error), 1)
        self.assertTrue(parser._files_to_close_on_error[0].closed)


if __name__ == "__main__":
    unittest.main()
