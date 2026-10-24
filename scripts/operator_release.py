"""Bounded read-only public provenance for Python source delivery.

Only this repository's canonical main/dev promotion and its exact workflow
attempts are accepted. A locally injected native fixture is not public proof.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from scripts import operator_backup as backup


REPOSITORY = "wongyiuming/FrontierCloud"
ORIGIN = "https://api.github.com"
PREFIX = "/repos/" + REPOSITORY
SHA = re.compile(r"[0-9a-f]{40}\Z")
PENDING = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})


class ReleaseError(backup.BackupError):
    def __init__(self, code, message, *, waiting=False):
        super().__init__(message)
        self.code, self.waiting = code, waiting


def refuse(code, message, *, waiting=False):
    raise ReleaseError(code, message, waiting=waiting)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            refuse("release_invalid_response", "GitHub returned duplicate JSON fields")
        result[key] = value
    return result


class GitHub:
    """Anonymous GET requests; no credentials or arbitrary response URLs used."""
    def __init__(self, *, opener=None):
        self.opener = opener if opener is not None else build_opener(NoRedirect())

    def get(self, path, **query):
        if not path.startswith(PREFIX + "/") or any(value in path for value in ("?", "#", "..", "\\")):
            refuse("release_invalid_request", "refusing an unexpected provenance endpoint")
        request = Request(ORIGIN + path + ("?" + urlencode(query) if query else ""), headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
            "User-Agent": "FrontierCloud-Python-Operator/1"}, method="GET")
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
        except HTTPError as error:
            code, headers = error.code, error.headers
            error.close()
            if code == 429 or code == 403 and (headers.get("X-RateLimit-Remaining") == "0" or headers.get("Retry-After")):
                refuse("release_rate_limited", "GitHub rate limiting prevented release verification")
            if code == 404:
                refuse("release_not_found", "the requested public release proof does not exist")
            if code == 403:
                refuse("release_access_denied", "GitHub denied access to public release proof")
            if 300 <= code < 400:
                refuse("release_repository_moved", "GitHub redirected the configured repository; review its identity")
            refuse("release_service_unavailable", "GitHub could not serve release proof (HTTP " + str(code) + ")")
        except (TimeoutError, socket.timeout):
            refuse("release_transport_timeout", "GitHub release verification timed out")
        except URLError as error:
            if isinstance(error.reason, ssl.SSLError):
                refuse("release_tls_failed", "GitHub certificate verification failed")
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                refuse("release_transport_timeout", "GitHub release verification timed out")
            refuse("release_transport_failed", "GitHub release proof could not be reached")
        except ssl.SSLError:
            refuse("release_tls_failed", "GitHub certificate verification failed")
        except OSError:
            refuse("release_transport_failed", "GitHub release proof could not be reached")
        if len(raw) > 8 * 1024 * 1024:
            refuse("release_response_limit", "GitHub release proof exceeds its response limit")
        try:
            return json.loads(raw, object_pairs_hook=_object)
        except (ValueError, UnicodeError):
            refuse("release_invalid_response", "GitHub returned invalid release JSON")


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        refuse("release_invalid_response", "release proof contains an invalid Git identity")
    return value


def _integer(value):
    if type(value) is not int or value <= 0:
        refuse("release_invalid_response", "release proof contains an invalid run identity")
    return value


def _items(api, path, *, key=None, **query):
    result = []
    for page in range(1, 6):
        response = api.get(path, **query, per_page=100, page=page)
        rows = response.get(key) if key and isinstance(response, dict) else response if key is None else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            refuse("release_invalid_response", "GitHub returned an incomplete proof list")
        result.extend(rows)
        if len(rows) < 100:
            if key and (type(response.get("total_count")) is not int or response["total_count"] != len(result)):
                refuse("release_proof_changed", "the GitHub proof list changed while being read")
            return result
    refuse("release_proof_limit", "release verification needs more than five proof pages")


def _commit(api, reference):
    value = api.get(PREFIX + "/commits/" + reference)
    if not isinstance(value, dict) or not isinstance(value.get("commit"), dict) or not isinstance(value["commit"].get("tree"), dict):
        refuse("release_invalid_response", "GitHub returned incomplete commit proof")
    return _sha(value.get("sha")), _sha(value["commit"]["tree"].get("sha"))


def _promotion(api, revision):
    pulls = _items(api, PREFIX + "/commits/" + revision + "/pulls")
    selected = []
    for pull in pulls:
        base, head = pull.get("base") or {}, pull.get("head") or {}
        if not isinstance(base, dict) or not isinstance(head, dict) or any(
                not isinstance(value.get("repo") or {}, dict) for value in (base, head)):
            refuse("release_invalid_response", "GitHub returned an invalid promotion identity")
        if pull.get("merged_at") and pull.get("merge_commit_sha") in {None, revision} and base.get("ref") == "main" and head.get("ref") == "dev" and (
                base.get("repo") or {}).get("full_name") == REPOSITORY and (head.get("repo") or {}).get("full_name") == REPOSITORY:
            try:
                merged = datetime.datetime.fromisoformat(pull["merged_at"].replace("Z", "+00:00"))
            except (ValueError, TypeError, AttributeError):
                refuse("release_invalid_response", "GitHub returned an invalid merge time")
            if merged.tzinfo is None or pull.get("state") != "closed":
                refuse("release_invalid_response", "GitHub returned an incomplete merged promotion")
            selected.append({"number": _integer(pull.get("number")), "source_sha": _sha(head.get("sha"))})
    if len(selected) != 1:
        refuse("release_promotion_missing", "main has no unique merged same-repository dev-to-main promotion")
    result = selected[0]
    # The associated-commit list can return merge_commit_sha=null for a real
    # merged PR. Obtain the authoritative full PR instead of treating null as
    # either proof of a merge or proof that no promotion exists.
    detail = api.get(PREFIX + "/pulls/" + str(result["number"]))
    if not isinstance(detail, dict) or detail.get("merged") is not True or detail.get("merge_commit_sha") not in {None, revision}:
        refuse("release_promotion_missing", "the full merged PR does not introduce the selected main commit")
    base, head = detail.get("base") or {}, detail.get("head") or {}
    if not isinstance(base, dict) or not isinstance(head, dict) or not isinstance(base.get("repo") or {}, dict) or not isinstance(head.get("repo") or {}, dict):
        refuse("release_invalid_response", "GitHub returned an invalid full promotion identity")
    if detail.get("number") != result["number"] or detail.get("state") != "closed" or not detail.get("merged_at") or (
            head.get("ref"), head.get("sha"), (head.get("repo") or {}).get("full_name"), base.get("ref"), (base.get("repo") or {}).get("full_name")) != (
            "dev", result["source_sha"], REPOSITORY, "main", REPOSITORY):
        refuse("release_proof_changed", "the full promotion no longer matches its commit association")
    if detail.get("merge_commit_sha") is None:
        # Some archived public PRs also omit this value in the full response.
        # Bind their normal merge structurally; association/null alone is not
        # permission to guess a merge commit or accept a merely equal tree.
        commit = api.get(PREFIX + "/commits/" + revision)
        parents = commit.get("parents") if isinstance(commit, dict) else None
        if not isinstance(parents, list) or len(parents) != 2 or any(not isinstance(row, dict) for row in parents) or (
                commit.get("sha"), parents[0].get("sha"), parents[1].get("sha")) != (
                revision, _sha(base.get("sha")), result["source_sha"]):
            refuse("release_merge_binding_unavailable", "the PR omits its merge commit and Git parents cannot bind this promotion")
    return result


def _run_state(run, scope):
    if run.get("status") in PENDING:
        refuse(scope + "_pending", scope.replace("_", " ") + " is still running", waiting=True)
    if run.get("status") != "completed":
        refuse("release_invalid_response", "GitHub returned an unknown workflow state")
    if run.get("conclusion") != "success":
        refuse(scope + "_failed", scope.replace("_", " ") + " did not pass")


def _run_identity(run):
    return {key: run.get(key) for key in ("id", "run_attempt", "run_number", "head_sha", "head_branch", "event", "status", "conclusion", "workflow_id")}


def _workflow(api, revision, branch, scope, required):
    path = PREFIX + "/actions/workflows/docker.yml/runs"
    runs = _items(api, path, key="workflow_runs", head_sha=revision, event="push", branch=branch)
    if any(not isinstance(row.get("repository") or {}, dict) for row in runs):
        refuse("release_invalid_response", "GitHub returned an invalid workflow repository")
    selected = [row for row in runs if row.get("head_sha") == revision and row.get("head_branch") == branch and row.get("event") == "push" and (
        row.get("repository") or {}).get("full_name") == REPOSITORY]
    if not selected:
        refuse(scope + "_missing", "no exact-commit " + scope.replace("_", " ") + " run exists")
    for row in selected:
        for key in ("id", "run_attempt", "run_number", "workflow_id"):
            _integer(row.get(key))
    if len({row["id"] for row in selected}) != len(selected):
        refuse("release_proof_changed", "the workflow list changed during pagination")
    run = max(selected, key=lambda row: (row["run_number"], row["id"]))
    _run_state(run, scope)
    jobs = _items(api, PREFIX + "/actions/runs/" + str(run["id"]) + "/attempts/" + str(run["run_attempt"]) + "/jobs", key="jobs")
    for name in required:
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success":
            refuse(scope + "_jobs_incomplete", "exact workflow attempt lacks a successful " + name + " job")
        if matches[0].get("head_sha") != revision or matches[0].get("run_id") != run["id"]:
            refuse("release_invalid_response", "a required job belongs to another commit or workflow run")
    latest = api.get(PREFIX + "/actions/runs/" + str(run["id"]))
    if not isinstance(latest, dict) or _run_identity(latest) != _run_identity(run):
        refuse("release_proof_changed", "the workflow attempt changed during release verification")
    again = _items(api, path, key="workflow_runs", head_sha=revision, event="push", branch=branch)
    if not again or any(type(row.get("run_number")) is not int or type(row.get("id")) is not int for row in again):
        refuse("release_invalid_response", "GitHub returned an invalid latest workflow list")
    newest = max(again, key=lambda row: (row["run_number"], row["id"]))
    if _run_identity(newest) != _run_identity(run):
        refuse("release_proof_changed", "a newer workflow run appeared during release verification")
    return {"id": run["id"], "attempt": run["run_attempt"], "number": run["run_number"], "required_jobs": list(required)}


def select_latest(api=None):
    api = api if api is not None else GitHub()
    revision, tree = _commit(api, "main")
    promotion = _promotion(api, revision)
    source, source_tree = _commit(api, promotion["source_sha"])
    if source != promotion["source_sha"] or source_tree != tree:
        refuse("release_tree_mismatch", "main differs from its exact promoted dev source tree")
    source_run = _workflow(api, source, "dev", "source_ci", ("source", "test-compose", "topology"))
    main_run = _workflow(api, revision, "main", "promotion_ci", ("promote-main", "topology"))
    if _commit(api, "main") != (revision, tree):
        refuse("release_main_changed", "main changed while release proof was being checked")
    proof = {"version": 1, "repository": REPOSITORY, "revision": revision, "source_sha": source,
             "tree_sha": tree, "promotion_pr": promotion["number"], "source_ci": source_run,
             "promotion_ci": main_run, "public_provenance_verified": True}
    proof["proof_sha256"] = hashlib.sha256(backup.json_bytes(proof)).hexdigest()
    return proof
