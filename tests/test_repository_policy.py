from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SOURCE_TREE_AVAILABLE = all((ROOT / path).exists() for path in (
    "README.md",
    "CONTRIBUTING.md",
    "ARCHITECTURE.md",
    "Dockerfile",
    "docker-compose.yaml",
    ".github/workflows/repository-policy.yml",
))


def _plain_markdown(value: str) -> str:
    return re.sub(r"[*_`]", "", value)


@unittest.skipUnless(
    _SOURCE_TREE_AVAILABLE,
    "repository-policy contracts run against the GitHub checkout, not the runtime image",
)
class RepositoryPolicyRegressionTests(unittest.TestCase):
    def test_main_prs_are_dev_to_main_only(self):
        workflow = (ROOT / ".github/workflows/repository-policy.yml").read_text(encoding="utf-8")
        self.assertIn('pull_request:', workflow)
        self.assertIn('branches: ["main", "gin_main"]', workflow)
        self.assertIn('HEAD_REF: ${{ github.head_ref }}', workflow)
        self.assertIn('HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}', workflow)
        self.assertIn('BASE_REPO: ${{ github.event.pull_request.base.repo.full_name }}', workflow)
        self.assertIn('main) expected_source=dev ;;', workflow)
        self.assertIn('gin_main) expected_source=gin_dev ;;', workflow)
        self.assertIn('[[ "$HEAD_REF" != "$expected_source" || "$HEAD_REPO" != "$BASE_REPO" ]]', workflow)
        self.assertIn('same-repository dev -> main or gin_dev -> gin_main', workflow)

    def test_noncanonical_branch_creation_is_detected(self):
        workflow = (ROOT / ".github/workflows/repository-policy.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  create:\s*$")
        self.assertIn("github.ref_type == 'branch'", workflow)
        self.assertIn('CREATED_REF: ${{ github.ref_name }}', workflow)
        self.assertIn('[[ "$CREATED_REF" != "dev" && "$CREATED_REF" != "main" && "$CREATED_REF" != "gin_dev" && "$CREATED_REF" != "gin_main" ]]', workflow)
        self.assertIn("new branches are prohibited", workflow)

    def test_no_new_branch_rule_is_explicit_and_release_flow_is_two_branch(self):
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        for content in (contributing, architecture):
            self.assertIn("Do not create any new branch", content)
            self.assertIn("dev", content)
            self.assertIn("main", content)
            self.assertIn("dev -> main", content)
            self.assertIn("gin_dev -> gin_main", content)
            self.assertIn("fast-forward", content)
        self.assertIn("only development branch", contributing)
        self.assertIn("never force-rewrite", architecture)

    def test_retired_compute_and_managed_rename_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        plain_architecture = _plain_markdown(architecture)
        self.assertNotIn("Storage, Compute, and Backup", readme)
        self.assertNotIn("worker APIs", readme)
        self.assertNotIn("No move API is currently provided", readme)
        self.assertIn("Compute Worker is retired", plain_architecture)
        self.assertIn("Folder rename", architecture)
        self.assertIn("lyrics/default.lrc", architecture)

    def test_auto_link_is_documented_as_non_destructive_fallback_automation(self):
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        for content in (architecture, contributing):
            self.assertIn("auto-link", content)
            self.assertIn("must never overwrite", content)
            self.assertIn("explicit", content)

    def test_upload_site_types_are_architectural_not_per_member_selection(self):
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        self.assertIn(
            "Admin media upload chooses a **site type**, never a concrete storage member",
            architecture,
        )
        self.assertIn("selector starts empty", architecture)
        self.assertIn("Historical media requires **no migration**", architecture)
        self.assertIn("low-saturation badges", architecture)
        self.assertIn("storage write lock", architecture)

    def test_media_mutation_fence_requires_single_web_process(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
        combined = dockerfile + "\n" + compose

        self.assertIn('ENTRYPOINT ["/app/frontiercloud"]', dockerfile)
        self.assertIn('CMD ["serve"]', dockerfile)
        self.assertFalse((ROOT / "Dockerfile.python").exists())
        self.assertFalse((ROOT / "docker-compose.python.yaml").exists())
        self.assertNotRegex(combined, re.compile(r"--workers(?:=|\s)", re.I))
        self.assertNotRegex(combined, re.compile(r"\bWEB_CONCURRENCY\b", re.I))
        self.assertNotRegex(combined, re.compile(r"\bgunicorn\b", re.I))
        self.assertIn("single native Go process", architecture)
        self.assertIn("distributed lock", architecture)
        self.assertIn("media mutation", architecture.lower())

    def test_wiki_is_external_and_python_is_not_a_deployment_profile(self):
        tracked = subprocess.check_output(["git", "ls-files", "--", "docs/wiki"], cwd=ROOT)
        self.assertEqual(tracked, b"")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/wongyiuming/FrontierCloud/wiki", readme)
        self.assertIn("/docs/wiki/", (ROOT / ".gitignore").read_text(encoding="utf-8"))
        for name in ("main.py", "updater/server.py"):
            self.assertIn('raise SystemExit("Python deployment is prohibited.',
                          (ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
