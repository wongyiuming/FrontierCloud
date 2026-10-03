"""Independent exact artifact proof for the isolated reference release agent."""
from __future__ import annotations

import http.client
import json
import os
import re
import ssl

from app.services.federation import release_manifest as manifests

REPOSITORY = "wongyiuming/FrontierCloud"
_SHA = re.compile(r"^[0-9a-f]{40}$")


def github_get(path: str):
    # Fixed authority, TLS verification, no environment proxy or redirect.
    connection = http.client.HTTPSConnection("api.github.com", timeout=5, context=ssl.create_default_context())
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "FrontierCloud-reference-updater"}
    token = os.environ.get("GITHUB_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        connection.request("GET", f"/repos/{REPOSITORY}" + path, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Artifact publication proof is unavailable")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Artifact publication proof exceeds limit")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=manifests._pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    finally:
        connection.close()


def verify_artifact(manifest: dict, branch: str = "main", get=None) -> dict:
    source = {"main": "dev", "gin_main": "gin_dev"}.get(branch)
    artifact = manifests.select(manifest, branch, source)
    target = artifact["commit_sha"]
    get = get or github_get
    production = get(f"/commits/{target}")
    if not isinstance(production, dict) or production.get("sha") != target:
        raise ValueError("Artifact commit proof does not match")
    tree = production.get("commit", {}).get("tree", {}).get("sha")
    if tree != artifact["tree_sha"]:
        raise ValueError("Artifact production tree does not match")
    pulls = get(f"/commits/{target}/pulls?per_page=100")
    if not isinstance(pulls, list) or len(pulls) >= 100:
        raise ValueError("Artifact PR association is incomplete")
    sources = set()
    for pull in pulls:
        if not isinstance(pull, dict):
            continue
        head, base = pull.get("head") or {}, pull.get("base") or {}
        sha = head.get("sha")
        if (pull.get("merged_at") and pull.get("merge_commit_sha") == target
                and base.get("ref") == branch and head.get("ref") == source
                and (head.get("repo") or {}).get("full_name") == REPOSITORY
                and isinstance(sha, str) and _SHA.fullmatch(sha)):
            sources.add(sha)
    if sources != {artifact["source_sha"]}:
        raise ValueError("Artifact has no unique exact reviewed source PR")
    source_sha = artifact["source_sha"]
    reviewed = get(f"/commits/{source_sha}")
    if reviewed.get("sha") != source_sha or reviewed.get("commit", {}).get("tree", {}).get("sha") != tree:
        raise ValueError("Artifact differs from reviewed source tree")
    payload = get(f"/actions/workflows/docker.yml/runs?event=push&head_sha={source_sha}&per_page=20")
    runs = payload.get("workflow_runs") or []
    matches = [row for row in runs if isinstance(row, dict) and row.get("head_sha") == source_sha
               and row.get("head_branch") == source and row.get("event") == "push"
               and type(row.get("run_number")) is int and row["run_number"] > 0]
    newest = max(matches, key=lambda row: row["run_number"], default={})
    evidence = {"available": True, "publishable": newest.get("status") == "completed" and newest.get("conclusion") == "success",
                "branch": branch, "source_branch": source, "sha": target, "ci_sha": source_sha,
                "tree_sha": tree, "status": newest.get("status"), "conclusion": newest.get("conclusion")}
    manifests.check_evidence(manifest, branch, source, evidence)
    return evidence
