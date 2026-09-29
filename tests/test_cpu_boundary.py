import unittest
from unittest.mock import patch

from scripts import check_cpu_boundary, check_cpu_quiescence


class CpuBoundaryTests(unittest.TestCase):
    def test_normal_async_and_streaming_io_are_allowed(self):
        source = """
import asyncio
async def copy(reader, writer):
    while chunk := await reader.read(1024 * 1024):
        writer.write(chunk)
        await asyncio.sleep(0)
"""
        self.assertEqual(check_cpu_boundary.scan_source(source), [])

    def test_process_execution_compression_and_compute_packages_are_rejected(self):
        sources = (
            "import subprocess\nsubprocess.run(['tool'])\n",
            "import gzip\ngzip.compress(b'data')\n",
            "import torch\ntorch.tensor([1])\n",
            "import asyncio\nasyncio.create_subprocess_exec('tool')\n",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(check_cpu_boundary.scan_source(source))

    def test_executor_offload_is_rejected_even_without_a_compute_import(self):
        source = "async def work(loop):\n    await loop.run_in_executor(None, lambda: 1)\n"
        self.assertIn("executor work", check_cpu_boundary.scan_source(source)[0])

    def test_repository_runtime_and_dependencies_pass_the_hard_boundary(self):
        self.assertEqual(check_cpu_boundary.main(), 0)

    @patch.object(check_cpu_quiescence.time, "sleep")
    @patch.object(check_cpu_quiescence, "cpu_percent", side_effect=[95, 88, 19, 18, 17, 16, 15])
    def test_short_spike_must_fall_for_five_samples(self, _cpu, _sleep):
        self.assertTrue(check_cpu_quiescence.wait_for_quiescence("web", timeout=30))

    @patch.object(check_cpu_quiescence.time, "sleep")
    @patch.object(
        check_cpu_quiescence,
        "cpu_percent",
        side_effect=[0.53, 0.24, 0.23, 27.06, 0.25, 0.25, 0.25, 0.25, 0.25],
    )
    def test_late_transient_spike_still_allows_five_sample_recovery(self, _cpu, _sleep):
        self.assertTrue(check_cpu_quiescence.wait_for_quiescence("web", timeout=30))

    @patch.object(check_cpu_quiescence.time, "sleep")
    @patch.object(check_cpu_quiescence.time, "monotonic", side_effect=[0, 0, 1, 2, 3, 4, 5, 6])
    @patch.object(check_cpu_quiescence, "cpu_percent", return_value=90)
    def test_continuous_cpu_never_counts_as_recovered(self, _cpu, _clock, _sleep):
        self.assertFalse(check_cpu_quiescence.wait_for_quiescence("web", timeout=5))


if __name__ == "__main__":
    unittest.main()
