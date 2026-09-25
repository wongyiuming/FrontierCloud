import unittest

from app.services import release_control


class ReleasePromotionTests(unittest.TestCase):
    def make_run(self, *, tree: str, number: int, status: str = "completed", conclusion: str | None = "success"):
        return {
            "head_branch": "dev",
            "event": "push",
            "head_sha": f"{number:040x}",
            "head_commit": {"tree_id": tree},
            "run_number": number,
            "status": status,
            "conclusion": conclusion,
        }

    def test_newest_matching_tree_is_authoritative(self):
        tree = "a" * 40
        runs = [
            self.make_run(tree=tree, number=9, conclusion="failure"),
            self.make_run(tree=tree, number=8, conclusion="success"),
        ]
        match = release_control._matching_ci_run(runs, tree)
        self.assertEqual(match["run_number"], 9)
        self.assertEqual(match["conclusion"], "failure")

    def test_unrelated_newer_dev_tree_is_ignored(self):
        target = "b" * 40
        runs = [
            self.make_run(tree="c" * 40, number=11),
            self.make_run(tree=target, number=10),
        ]
        match = release_control._matching_ci_run(runs, target)
        self.assertEqual(match["run_number"], 10)

    def test_non_dev_or_non_push_run_cannot_promote(self):
        tree = "d" * 40
        wrong_branch = self.make_run(tree=tree, number=4)
        wrong_branch["head_branch"] = "main"
        wrong_event = self.make_run(tree=tree, number=3)
        wrong_event["event"] = "workflow_dispatch"
        self.assertIsNone(release_control._matching_ci_run([wrong_branch, wrong_event], tree))

    def test_invalid_tree_never_matches(self):
        self.assertIsNone(release_control._matching_ci_run([], "not-a-sha"))


if __name__ == "__main__":
    unittest.main()
