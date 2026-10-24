"""Public proof selection must never substitute old green runs for new failures."""
import copy
import io
import json
import ssl
import unittest
from urllib.error import HTTPError, URLError

from scripts import frontiercloud_operator as entry
from scripts import operator_release as release
from unittest.mock import patch


MAIN, SOURCE, TREE = "1" * 40, "2" * 40, "3" * 40


class PublicFixture:
    def __init__(self):
        self.calls = []
        self.commits = {"main": {"sha": MAIN, "commit": {"tree": {"sha": TREE}}, "parents": [{"sha": "4" * 40}, {"sha": SOURCE}]},
                        SOURCE: {"sha": SOURCE, "commit": {"tree": {"sha": TREE}}}}
        self.commits[MAIN] = self.commits["main"]
        self.pulls = [{"number": 12, "state": "closed", "merged_at": "2026-10-20T00:00:00Z", "merge_commit_sha": MAIN,
                       "base": {"ref": "main", "sha": "4" * 40, "repo": {"full_name": release.REPOSITORY}},
                       "head": {"ref": "dev", "sha": SOURCE, "repo": {"full_name": release.REPOSITORY}}}]
        self.detail = copy.deepcopy(self.pulls[0]) | {"merged": True}
        self.runs = {}
        self.jobs = {}
        for number, branch, sha, names in ((10, "dev", SOURCE, ("source", "test-compose", "topology")),
                                           (11, "main", MAIN, ("promote-main", "topology"))):
            self.runs[branch] = [{"id": number, "run_attempt": 2, "run_number": number, "head_sha": sha,
                                   "head_branch": branch, "event": "push", "workflow_id": 123,
                                   "status": "completed", "conclusion": "success", "repository": {"full_name": release.REPOSITORY}}]
            self.jobs[number] = [{"name": name, "status": "completed", "conclusion": "success", "head_sha": sha, "run_id": number} for name in names]
        self.after = None

    def get(self, path, **query):
        self.calls.append((path, query))
        if self.after:
            self.after(self, path, query)
        suffix = path.removeprefix(release.PREFIX)
        if suffix == "/pulls/12":
            return copy.deepcopy(self.detail)
        if suffix.startswith("/commits/") and suffix.endswith("/pulls"):
            return copy.deepcopy(self.pulls if query["page"] == 1 else [])
        if suffix.startswith("/commits/"):
            return copy.deepcopy(self.commits[suffix.removeprefix("/commits/")])
        if suffix.endswith("/runs"):
            rows = self.runs[query["branch"]]
            return {"total_count": len(rows), "workflow_runs": copy.deepcopy(rows[(query["page"]-1)*100:query["page"]*100])}
        parts = suffix.split("/")
        number = int(parts[3])
        if suffix.endswith("/jobs"):
            if parts[4:6] != ["attempts", "2"]:
                raise AssertionError("did not pin the exact workflow attempt")
            return {"total_count": len(self.jobs[number]), "jobs": copy.deepcopy(self.jobs[number])}
        return copy.deepcopy(next(row for rows in self.runs.values() for row in rows if row["id"] == number))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = PublicFixture()

    def refused(self, code):
        with self.assertRaises(release.ReleaseError) as caught:
            release.select_latest(self.fixture)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_exact_merged_tree_and_each_required_exact_attempt(self):
        proof = release.select_latest(self.fixture)
        self.assertEqual((proof["revision"], proof["source_sha"], proof["tree_sha"]), (MAIN, SOURCE, TREE))
        self.assertEqual(proof["source_ci"]["attempt"], 2)
        self.assertEqual(proof["promotion_pr"], 12)
        self.assertTrue(proof["public_provenance_verified"])
        self.assertEqual(len(proof["proof_sha256"]), 64)

    def test_pending_source_ci_has_a_waiting_state(self):
        for state in release.PENDING:
            with self.subTest(state=state):
                self.fixture.runs["dev"][0].update(status=state, conclusion=None)
                self.assertTrue(self.refused("source_ci_pending").waiting)

    def test_null_merge_in_association_requires_authoritative_full_pr(self):
        self.fixture.pulls[0]["merge_commit_sha"] = None
        self.assertEqual(release.select_latest(self.fixture)["promotion_pr"], 12)
        self.fixture.detail["merge_commit_sha"] = "4" * 40
        self.refused("release_promotion_missing")
        self.fixture.detail.update(merge_commit_sha=MAIN, merged=False)
        self.refused("release_promotion_missing")

    def test_full_pr_cannot_change_reviewed_source_or_repository(self):
        self.fixture.detail["head"]["sha"] = "4" * 40
        self.refused("release_proof_changed")
        self.fixture.detail["head"].update(sha=SOURCE)
        self.fixture.detail["head"]["repo"]["full_name"] = "foreign/FrontierCloud"
        self.refused("release_proof_changed")

    def test_null_full_merge_requires_exact_git_merge_parents(self):
        self.fixture.pulls[0]["merge_commit_sha"] = None
        self.fixture.detail["merge_commit_sha"] = None
        self.assertEqual(release.select_latest(self.fixture)["revision"], MAIN)
        self.fixture.commits[MAIN]["parents"].reverse()
        self.refused("release_merge_binding_unavailable")

    def test_pending_promotion_and_failed_source_are_distinct(self):
        self.fixture.runs["main"][0].update(status="in_progress", conclusion=None)
        self.refused("promotion_ci_pending")
        self.fixture.runs["dev"][0].update(status="completed", conclusion="failure")
        self.refused("source_ci_failed")

    def test_latest_failed_run_cannot_fall_back_to_older_success(self):
        old = copy.deepcopy(self.fixture.runs["dev"][0])
        old.update(id=9, run_number=9)
        self.fixture.runs["dev"].append(old)
        self.fixture.runs["dev"][0]["conclusion"] = "failure"
        self.refused("source_ci_failed")

    def test_missing_workflow_is_not_reported_as_pending_or_as_missing_images(self):
        self.fixture.runs["dev"] = []
        self.assertFalse(self.refused("source_ci_missing").waiting)

    def test_missing_or_skipped_native_job_cannot_be_success(self):
        self.fixture.jobs[10][1]["conclusion"] = "skipped"
        self.refused("source_ci_jobs_incomplete")
        self.fixture.jobs[10].pop(1)
        self.refused("source_ci_jobs_incomplete")

    def test_wrong_commit_or_run_job_is_invalid(self):
        self.fixture.jobs[10][0]["head_sha"] = MAIN
        self.refused("release_invalid_response")
        self.fixture.jobs[10][0].update(head_sha=SOURCE, run_id=11)
        self.refused("release_invalid_response")

    def test_duplicate_required_job_is_not_accepted(self):
        self.fixture.jobs[10].append(copy.deepcopy(self.fixture.jobs[10][0]))
        self.refused("source_ci_jobs_incomplete")

    def test_same_repository_dev_main_and_exact_merge_are_required(self):
        original = copy.deepcopy(self.fixture.pulls)
        for mutate in (lambda pull: pull["head"]["repo"].update(full_name="foreign/FrontierCloud"),
                       lambda pull: pull["base"].update(ref="release"),
                       lambda pull: pull.update(merge_commit_sha=SOURCE),
                       lambda pull: pull.update(merged_at=None)):
            self.fixture.pulls = copy.deepcopy(original)
            mutate(self.fixture.pulls[0])
            self.refused("release_promotion_missing")

    def test_ambiguous_promotions_and_changed_reviewed_tree_refuse(self):
        self.fixture.pulls.append(copy.deepcopy(self.fixture.pulls[0]))
        self.refused("release_promotion_missing")
        self.fixture.pulls.pop()
        self.fixture.commits[SOURCE]["commit"]["tree"]["sha"] = "4" * 40
        self.refused("release_tree_mismatch")

    def test_unknown_run_state_and_boolean_run_identity_refuse(self):
        self.fixture.runs["dev"][0]["status"] = "mystery"
        self.refused("release_invalid_response")
        self.fixture.runs["dev"][0].update(status="completed", id=True)
        self.refused("release_invalid_response")

    def test_new_attempt_or_new_run_during_verification_refuse(self):
        def rerun(api, path, query):
            if path.endswith("/actions/runs/10"):
                api.runs["dev"][0]["run_attempt"] = 3
        self.fixture.after = rerun
        self.refused("release_proof_changed")
        self.fixture = PublicFixture()
        def new_run(api, path, query):
            if path.endswith("/actions/runs/10"):
                latest = copy.deepcopy(api.runs["dev"][0])
                latest.update(id=20, run_number=20, status="queued", conclusion=None)
                api.runs["dev"].append(latest)
        self.fixture.after = new_run
        self.refused("release_proof_changed")

    def test_main_ref_moving_during_verification_refuse(self):
        count = 0
        def advance(api, path, query):
            nonlocal count
            if path.endswith("/commits/main"):
                count += 1
                if count == 2:
                    api.commits["main"]["sha"] = "4" * 40
        self.fixture.after = advance
        self.refused("release_main_changed")

    def test_workflow_pagination_does_not_hide_newest_failure(self):
        first = self.fixture.runs["dev"][0]
        self.fixture.runs["dev"] = [dict(first, id=1000+n, run_number=1000+n) for n in range(101)]
        self.fixture.runs["dev"][-1]["conclusion"] = "failure"
        self.refused("source_ci_failed")
        self.assertTrue(any(query.get("page") == 2 for _, query in self.fixture.calls))

    def test_cli_waits_without_mutating_deployment_or_requesting_version(self):
        error = release.ReleaseError("promotion_ci_pending", "promotion ci is still running", waiting=True)
        output = io.StringIO()
        with patch.object(entry, "select_latest", side_effect=error), patch("sys.stdout", output):
            self.assertEqual(entry.main(["candidate"]), 3)
        result = json.loads(output.getvalue())
        self.assertEqual(result["state"], "waiting")
        self.assertFalse(result["deployment_changed"])


class FakeOpener:
    def __init__(self, value=b"{}", error=None):
        self.value, self.error, self.requests = value, error, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.value)


class TransportTests(unittest.TestCase):
    def test_only_pinned_https_repository_get_without_authorization(self):
        opener = FakeOpener()
        self.assertEqual(release.GitHub(opener=opener).get(release.PREFIX + "/commits/main"), {})
        request, timeout = opener.requests[0]
        self.assertTrue(request.full_url.startswith("https://api.github.com/repos/wongyiuming/FrontierCloud/"))
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 15)
        with self.assertRaises(release.ReleaseError):
            release.GitHub(opener=opener).get("https://foreign.invalid/secret")

    def test_rate_limit_not_found_denial_redirect_and_server_failure_are_distinct(self):
        for status, headers, code in ((429, {}, "release_rate_limited"),
                                       (403, {"X-RateLimit-Remaining": "0"}, "release_rate_limited"),
                                       (403, {"Retry-After": "10"}, "release_rate_limited"),
                                       (404, {}, "release_not_found"), (403, {}, "release_access_denied"),
                                       (301, {}, "release_repository_moved"), (503, {}, "release_service_unavailable")):
            with self.subTest(status=status, headers=headers):
                error = HTTPError("https://secret.invalid", status, "secret response", headers, io.BytesIO(b"secret"))
                with self.assertRaises(release.ReleaseError) as caught:
                    release.GitHub(opener=FakeOpener(error=error)).get(release.PREFIX + "/commits/main")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))

    def test_tls_timeout_and_connection_errors_are_distinct_and_sanitized(self):
        for error, code in ((URLError(ssl.SSLCertVerificationError("private")), "release_tls_failed"),
                            (URLError(TimeoutError("private")), "release_transport_timeout"),
                            (TimeoutError("private"), "release_transport_timeout"),
                            (URLError("private endpoint"), "release_transport_failed")):
            with self.subTest(code=code):
                with self.assertRaises(release.ReleaseError) as caught:
                    release.GitHub(opener=FakeOpener(error=error)).get(release.PREFIX + "/commits/main")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("private", str(caught.exception))

    def test_duplicate_invalid_and_excessive_json_refuse(self):
        for value, code in ((b'{"id":1,"id":2}', "release_invalid_response"),
                            (b"not-json", "release_invalid_response"),
                            (b" " * (8*1024*1024+1), "release_response_limit")):
            with self.assertRaises(release.ReleaseError) as caught:
                release.GitHub(opener=FakeOpener(value)).get(release.PREFIX + "/commits/main")
            self.assertEqual(caught.exception.code, code)

    def test_no_redirect_handler_never_forwards_a_request(self):
        self.assertIsNone(release.NoRedirect().redirect_request(None, None, 302, "", {}, "https://foreign.invalid"))
