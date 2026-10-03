"""Development-only schema oracle; the Go image embeds JSON, not Python."""
import json
from pathlib import Path
import unittest

import main


class SharedOpenAPIContractTests(unittest.TestCase):
    def test_reviewed_language_neutral_schema_matches_effective_reference(self):
        path = Path(__file__).resolve().parents[1] / "protocol/v2/openapi.json"
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), main.app.openapi())


if __name__ == "__main__":
    unittest.main()
