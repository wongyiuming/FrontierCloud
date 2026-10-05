"""Keep hosted CI lightweight, even when acceptance scripts evolve."""
from pathlib import Path
import unittest

from scripts.check_ci_budget import check_workflows, inspect_workflow


class CIBudgetTests(unittest.TestCase):
    def test_all_actual_workflows_fit_budget(self):
        self.assertEqual(check_workflows(Path(__file__).resolve().parents[1] / ".github/workflows"), [])

    def test_missing_extended_or_dynamic_timeout_fails(self):
        for value in ("", "    timeout-minutes: 65\n", "    timeout-minutes: ${{ inputs.limit }}\n"):
            self.assertTrue(inspect_workflow("name: test\njobs:\n  check:\n" + value, "fixture"))

    def test_heavywork_and_chains_cannot_hide_behind_short_timeout(self):
        for step in ("bash scripts/test-mixed-runtime.sh", "docker build .", "nohup test &", "federation_stack.py", "bash scripts/test-go-updater.sh"):
            self.assertTrue(inspect_workflow("name: test\njobs:\n  check:\n    timeout-minutes: 3\n    run: " + step, "fixture"))
        self.assertTrue(inspect_workflow("name: test\njobs:\n  check:\n    timeout-minutes: 3\n    needs: previous\n", "fixture"))
