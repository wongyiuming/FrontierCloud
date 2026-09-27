from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryPolicyRegressionTests(unittest.TestCase):
    def test_main_prs_are_dev_to_main_only(self):
        workflow = (ROOT / ".github/workflows/repository-policy.yml").read_text(encoding="utf-8")
        self.assertIn('pull_request:', workflow)
        self.assertIn('branches: ["main"]', workflow)
        self.assertIn('HEAD_REF: ${{ github.head_ref }}', workflow)
        self.assertIn('HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}', workflow)
        self.assertIn('BASE_REPO: ${{ github.event.pull_request.base.repo.full_name }}', workflow)
        self.assertIn('[[ "$HEAD_REF" != "dev" || "$HEAD_REPO" != "$BASE_REPO" ]]', workflow)
        self.assertIn('same-repository dev -> main', workflow)

    def test_no_new_branch_rule_is_explicit_and_release_flow_is_two_branch(self):
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for content in (contributing, architecture):
            self.assertIn("Do not create any new branch", content)
            self.assertIn("dev", content)
            self.assertIn("main", content)
            self.assertIn("dev -> main", content)
            self.assertIn("fast-forward", content)
        self.assertIn("only development branch", contributing)
        self.assertIn("never force-rewrite", architecture)

    def test_retired_compute_and_managed_rename_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertNotIn("Storage, Compute, and Backup", readme)
        self.assertNotIn("worker APIs", readme)
        self.assertNotIn("No move API is currently provided", readme)
        self.assertIn("Compute Worker is retired", architecture)
        self.assertIn("Folder rename", architecture)
        self.assertIn("lyrics/default.lrc", architecture)


if __name__ == "__main__":
    unittest.main()
