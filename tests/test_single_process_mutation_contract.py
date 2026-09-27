from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SingleProcessMutationFenceTests(unittest.TestCase):
    def test_production_web_stays_single_process(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        combined = dockerfile + "\n" + compose

        self.assertIn('CMD ["uvicorn", "main:app"', dockerfile)
        self.assertNotRegex(combined, re.compile(r"--workers(?:=|\s)", re.I))
        self.assertNotRegex(combined, re.compile(r"\bWEB_CONCURRENCY\b", re.I))
        self.assertNotRegex(combined, re.compile(r"\bgunicorn\b", re.I))

    def test_architecture_explains_why_multi_worker_is_forbidden(self):
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn("single ASGI worker", architecture)
        self.assertIn("distributed lock", architecture)
        self.assertIn("media mutation", architecture.lower())


if __name__ == "__main__":
    unittest.main()
