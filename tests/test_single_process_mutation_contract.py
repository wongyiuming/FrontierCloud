from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    (ROOT / "Dockerfile").exists() and (ROOT / "ARCHITECTURE.md").exists(),
    "source-only contract runs against the GitHub checkout",
)
class SingleProcessMutationFenceTests(unittest.TestCase):
    def test_contract_moved_to_repository_policy_suite(self):
        policy = (ROOT / "tests/test_repository_policy.py").read_text(encoding="utf-8")
        self.assertIn("test_media_mutation_fence_requires_single_web_process", policy)


if __name__ == "__main__":
    unittest.main()
