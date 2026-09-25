import unittest

from app.services import release_control


class ReleasePromotionTests(unittest.TestCase):
    def make_run(self, *, sha: str, number: int, status: str = "completed", conclusion: str | None = "success"):
        return {
            "head_branch": "dev",
            "event": "push",
            "head_sha": sha,
            "run_number": number,
            "status": status,
            "conclusion": conclusion,
        }

    def test_newest_exact_dev_sha_is_authoritative(self):
        sha = "a" * 40
        runs = [
            self.make_run(sha=sha, number=9, conclusion="failure"),
            self.make_run(sha=sha, number=8, conclusion="success"),
        ]
        match = release_control._matching_ci_run(runs, sha)
        self.assertEqual(match["run_number"], 9)
        self.assertEqual(match["conclusion"], "failure")

    def test_unrelated_dev_sha_is_ignored(self):
        target = "b" * 40
        runs = [
            self.make_run(sha="c" * 40, number=11),
            self.make_run(sha=target, number=10),
        ]
        match = release_control._matching_ci_run(runs, target)
        self.assertEqual(match["run_number"], 10)

    def test_non_dev_or_non_push_run_cannot_promote(self):
        sha = "d" * 40
        wrong_branch = self.make_run(sha=sha, number=4)
        wrong_branch["head_branch"] = "main"
        wrong_event = self.make_run(sha=sha, number=3)
        wrong_event["event"] = "workflow_dispatch"
        self.assertIsNone(release_control._matching_ci_run([wrong_branch, wrong_event], sha))

    def test_invalid_source_sha_never_matches(self):
        self.assertIsNone(release_control._matching_ci_run([], "not-a-sha"))


if __name__ == "__main__":
    unittest.main()
