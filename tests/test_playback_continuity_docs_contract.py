from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlaybackContinuityDocumentationContractTests(unittest.TestCase):
    def test_core_contract_is_documented_in_md_and_wiki(self):
        design = (ROOT / "docs/playback-continuity.md").read_text(encoding="utf-8")
        wiki = (ROOT / "docs/wiki/Playback-Continuity.md").read_text(encoding="utf-8")
        sidebar = (ROOT / "docs/wiki/_Sidebar.md").read_text(encoding="utf-8")
        for document in (design, wiki):
            self.assertIn("200 ms", document)
            self.assertIn("standby", document.lower())
            self.assertIn("ended fallback", document.lower())
            self.assertIn("core", document.lower())
            self.assertIn("temporary", document.lower())
        self.assertIn("[Playback Continuity](Playback-Continuity)", sidebar)

    def test_ci_executes_runtime_and_source_continuity_contracts(self):
        workflow = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
        network_smoke = (ROOT / "tests/network_observation_smoke.mjs").read_text(encoding="utf-8")
        self.assertIn("node tests/network_observation_smoke.mjs", workflow)
        self.assertIn("./playback_handoff_smoke.mjs", network_smoke)
        self.assertIn("tests/test_playback_continuity_docs_contract.py", workflow)
        self.assertIn("! -name 'test_playback_continuity_docs_contract.py'", workflow)


if __name__ == "__main__":
    unittest.main()
