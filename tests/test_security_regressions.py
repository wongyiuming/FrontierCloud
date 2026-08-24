import io
import tarfile
import unittest
import zipfile
from unittest.mock import patch

import py7zr
from fastapi import HTTPException, UploadFile
from PIL import Image

from app.api.v1 import endpoints
from app.core import utils
from app.core.admin_log import sanitize_log_value


class ArchiveSecurityTests(unittest.TestCase):
    @staticmethod
    def _zip(entries: dict[str, bytes]) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr(name, content)
        return output.getvalue()

    def test_zip_path_traversal_is_rejected(self):
        payload = self._zip({"../outside.txt": b"owned"})
        with self.assertRaises(utils.UnsafeArchiveError):
            utils.process_any_archive(payload, "mark", ".zip")

    def test_tar_symlink_is_rejected(self):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            link = tarfile.TarInfo("leak.txt")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            archive.addfile(link)
        with self.assertRaises(utils.UnsafeArchiveError):
            utils.process_any_archive(output.getvalue(), "mark", ".tar.gz")

    def test_high_compression_ratio_is_rejected(self):
        payload = self._zip({"large.txt": b"A" * (2 * 1024 * 1024)})
        with self.assertRaises(utils.ArchiveLimitError):
            utils.process_any_archive(payload, "mark", ".zip")

    def test_safe_archive_is_returned_as_safe_zip(self):
        payload = self._zip({"folder/readme.txt": b"safe"})
        result = utils.process_any_archive(payload, "mark", ".zip")
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            self.assertEqual(archive.namelist(), ["folder/readme.txt"])
            self.assertEqual(archive.read("folder/readme.txt"), b"safe")

    def test_safe_seven_zip_is_validated_and_processed(self):
        payload = io.BytesIO()
        with py7zr.SevenZipFile(payload, "w") as archive:
            archive.writestr(b"safe", "folder/readme.txt")
        result = utils.process_any_archive(payload.getvalue(), "mark", ".7z")
        with zipfile.ZipFile(io.BytesIO(result)) as archive:
            self.assertEqual(archive.namelist(), ["folder/readme.txt"])
            self.assertEqual(archive.read("folder/readme.txt"), b"safe")

    def test_oversized_image_dimensions_are_rejected(self):
        image = Image.new("RGB", (4, 4), "white")
        payload = io.BytesIO()
        image.save(payload, format="PNG")
        image_bytes = payload.getvalue() + b"\x00" * 6000
        with (
            patch.object(utils.settings, "WATERMARK_MAX_IMAGE_PIXELS", 15),
            self.assertRaises(utils.ProcessingLimitError),
        ):
            utils.process_single_image(image_bytes, "mark")

        with (
            patch.object(utils.settings, "WATERMARK_MAX_IMAGE_PIXELS", 15),
            self.assertRaises(utils.ProcessingLimitError),
        ):
            utils.dispatch_task(("large.png", image_bytes), "mark")


class WatermarkBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_size_is_enforced_while_streaming(self):
        upload = UploadFile(filename="test.pdf", file=io.BytesIO(b"12345"), size=None)
        with (
            patch.object(endpoints.settings, "WATERMARK_MAX_UPLOAD_FILE_SIZE", 4),
            self.assertRaises(HTTPException) as raised,
        ):
            await endpoints._read_upload_limited(upload, 4)
        self.assertEqual(raised.exception.status_code, 413)

    def test_download_header_cannot_be_split(self):
        header = endpoints._attachment_header("report\r\nX-Evil: yes.pdf")
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertNotIn("X-Evil: yes", header)

    def test_multipart_filename_cannot_create_zip_traversal(self):
        with self.assertRaises(HTTPException):
            endpoints._validated_upload_name("../escape.pdf")


class XSSAndLogIntegrityTests(unittest.TestCase):
    def test_log_values_cannot_inject_new_records_or_terminal_controls(self):
        value = sanitize_log_value("safe\n[ADMIN] forged\x1b[2J")
        self.assertEqual(value, "safe\\n[ADMIN] forged\\u{1b}[2J")
        self.assertNotIn("\n", value)
        self.assertNotIn("\x1b", value)


if __name__ == "__main__":
    unittest.main()
