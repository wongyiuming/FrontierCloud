import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeDataContractTests(unittest.TestCase):
    def test_runtime_data_tree_is_not_tracked_by_git(self):
        result = subprocess.run(
            ["git", "ls-files", "--", "data/"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        tracked = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(
            tracked,
            [],
            "Runtime data must stay outside Git; bootstrap required files at runtime instead",
        )


if __name__ == "__main__":
    unittest.main()
