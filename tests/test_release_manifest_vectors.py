"""Shared offline manifest oracle; importing it does not enable mixed releases."""
import copy
import json
from pathlib import Path
import unittest

from app.services.federation import protocol as p
from app.services.federation import release_manifest as m


class ReleaseManifestVectors(unittest.TestCase):
    def fixture(self):
        root = Path(__file__).resolve().parents[1]
        return json.loads((root / "protocol/v2/vectors/release-manifest.json").read_text(encoding="utf-8"))

    def test_shared_strict_manifest_vectors_and_private_resolution(self):
        for vector in self.fixture()["cases"]:
            with self.subTest(vector["name"]):
                raw = vector["raw"].encode() if "raw" in vector else json.dumps(vector["manifest"]).encode()
                if not vector["valid"]:
                    with self.assertRaises(p.ProtocolError):
                        m.parse(raw)
                    continue
                value = m.parse(raw)
                self.assertEqual(m.identifier(value), "6db944731759d32042875701c66049ff9df0bfacd7f10eb8920ef2b7c9c25e70")
                self.assertEqual(m.select(value, "main", "dev")["commit_sha"], "a" * 40)
                self.assertEqual(m.select(value, "gin_main", "gin_dev")["commit_sha"], "d" * 40)
                selected = m.select(value, "main", "dev")
                selected.clear()
                self.assertIn("commit_sha", value["artifacts"]["main"])
                with self.assertRaises(p.ProtocolError):
                    m.select(value, "gin_main", "dev")
        with self.assertRaises(p.ProtocolError):
            m.parse(b" " * (m.MAX_MANIFEST_BYTES + 1))

    def test_each_selected_artifact_requires_its_own_exact_ci_proof(self):
        value = self.fixture()["cases"][0]["manifest"]
        for branch, source in (("main", "dev"), ("gin_main", "gin_dev")):
            artifact = m.select(value, branch, source)
            evidence = {"available": True, "publishable": True, "branch": branch, "source_branch": source,
                        "sha": artifact["commit_sha"], "ci_sha": artifact["source_sha"],
                        "tree_sha": artifact["tree_sha"], "status": "completed", "conclusion": "success"}
            m.check_evidence(value, branch, source, evidence)
            for key in evidence:
                bad = copy.deepcopy(evidence)
                bad[key] = False if type(bad[key]) is bool else "wrong"
                with self.subTest(branch=branch, key=key), self.assertRaises(p.ProtocolError):
                    m.check_evidence(value, branch, source, bad)
        self.assertNotIn(m.MANIFEST_CAPABILITY, p.BASELINE_CAPABILITIES)


if __name__ == "__main__":
    unittest.main()
