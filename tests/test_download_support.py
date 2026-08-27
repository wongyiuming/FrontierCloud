from io import StringIO
import unittest

from auto_download.download_support import (
    ConciseYtdlpLogger,
    DownloadProgress,
    SyncReport,
)


class DownloadSupportTests(unittest.TestCase):
    def test_logger_suppresses_routine_messages(self):
        output = StringIO()
        logger = ConciseYtdlpLogger(output)
        logger.debug("[debug] extractor detail")
        logger.info("routine status")
        logger.warning("retrying")
        logger.error("failed")
        self.assertEqual(output.getvalue(), "[警告] retrying\n[错误] failed\n")

    def test_progress_prints_speed_without_native_progress_line(self):
        output = StringIO()
        progress = DownloadProgress(output, minimum_interval=0)
        progress({"status": "downloading", "speed": 2 * 1024 * 1024})
        self.assertEqual(output.getvalue(), "[速度] 2.0 MiB/s\n")

    def test_report_separates_skipped_and_added_items(self):
        output = StringIO()
        SyncReport(skipped=["旧歌"], added=["新歌"]).print(output)
        rendered = output.getvalue()
        self.assertIn("已存在，跳过（1）", rendered)
        self.assertIn("本次新增（1）", rendered)
        self.assertIn("远端已无，本地删除（0）", rendered)


if __name__ == "__main__":
    unittest.main()
