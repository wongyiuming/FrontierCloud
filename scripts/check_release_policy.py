#!/usr/bin/env python3
"""Protect native deployment, reviewed release provenance and hosted CI limits."""
from pathlib import Path
import ast
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")

def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)

release = read("internal/release/verification.go")
updater = read("internal/updater/source.go")
executor = read("internal/updater/executor.go")
workflow = read(".github/workflows/docker.yml")
compose = read("docker-compose.yaml")
web = read("Dockerfile")
agent = read("updater/Dockerfile")
require('Policy{"main", "dev"}' in release and 'Policy{"gin_main", "gin_dev"}' in release,
        "Release policy must retain both authorized provenance pairs")
for token in ('p.MergeSHA == sha', 'p.Base.Ref == v.policy.Branch',
              'p.Head.Ref == v.policy.Source', 'p.Head.Repo.FullName == "wongyiuming/FrontierCloud"',
              'reviewed.Commit.Tree.SHA != tree', 'head_sha="+source',
              'r.Branch != v.policy.Source', 'r.Event != "push"', 'r.SHA != source'):
    require(token in release, f"Exact reviewed source provenance missing: {token}")
require('refs/remotes/origin/' in updater and '"+refs/heads/"' in updater,
        "Updater must fetch an explicit production remote-tracking ref")
require('target != head' in updater and '"merge-base", "--is-ancestor"' in updater,
        "Upgrade HEAD and rollback ancestry must fail closed")
require('clearForceOpen' in executor, "Release maintenance must remove stale open override")
require('cluster_convergence_needed' in read("internal/release/coordinator.go"),
        "Partial convergence must remain observable/retryable")
for token in ('promote-main:', 'promote-gin-main:', 'sourceTree !== mainTree',
              'head_sha: sourceSha', 'head_sha: context.sha', "pr?.base?.ref === 'main'",
              "pr?.head?.ref === 'dev'", "pr?.base?.ref === 'gin_main'",
              "pr?.head?.ref === 'gin_dev'", 'listPullRequestsAssociatedWithCommit'):
    require(token in workflow, f"Reviewed CI promotion guard missing: {token}")
require('github.paginate' not in workflow, "Promotion lookup must remain bounded")

require(web == read("Dockerfile.gin") and compose == read("docker-compose.gin.yaml"),
        "Native aliases must match defaults")
require(agent == read("updater/Dockerfile.gin"), "Updater alias must be native")
for path in ("Dockerfile.python", "docker-compose.python.yaml",
             "scripts/test-mixed-runtime.sh", "scripts/test-mixed-release.sh",
             "scripts/test-store-interop.sh", "tests/store.Dockerfile"):
    require(not (ROOT / path).exists(), f"Retired runtime/interop entrypoint exists: {path}")
for path in ("main.py", "updater/server.py"):
    require('raise SystemExit("Python deployment is prohibited.' in read(path),
            f"Reference executable must reject deployment: {path}")
require('FROM golang:1.26.0-bookworm' in web and 'FROM golang:1.26.0-bookworm' in agent,
        "Native builder base must remain patch-pinned")
for token, path in (('FROM nginx:1.30.4-alpine', "nginx/Dockerfile"),
                    ('image: redis:7.4.11-alpine', "docker-compose.yaml"),
                    ('image: mysql:8.4.11', "docker-compose.gin-mysql.yaml"),
                    ('image: coturn/coturn:4.17.2-r0-alpine', "docker-compose.yaml")):
    require(token in read(path), f"Patch-pinned runtime base missing: {path}")
require('ARG FRONTIERCLOUD_RUNTIME=go' in read("nginx/Dockerfile"), "Edge defaults must be native")
require('  mysql:' not in compose and 'DB_TYPE: ${DB_TYPE:-sqlite}' in compose,
        "Default native SQLite must not require MySQL")
require('command: [init-secrets]' in compose and 'command: [init-media]' in compose
        and 'dockerfile: updater/Dockerfile.gin' in compose, "Initializers/updater must be native")
require('RELEASE_BRANCH: ${RELEASE_BRANCH:-gin_main}' in compose
        and 'RELEASE_SOURCE_BRANCH: ${RELEASE_SOURCE_BRANCH:-gin_dev}' in compose,
        "Default release profile must remain fixed pending separate consolidation")
for token in ("app/", "main.py", "tests/", "scripts/"):
    require(token in read(".dockerignore").splitlines(), f"Build context exclusion missing: {token}")

updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
web_block = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]
require('updater_control:/run/frontiercloud-updater\n' in updater_block,
        "Updater socket volume must be writable")
require('updater_control:/run/frontiercloud-updater:ro' in web_block,
        "Web socket volume must be read-only")
for path in ("Dockerfile", "nginx/Dockerfile", "updater/Dockerfile"):
    require("COPY --chmod=" not in read(path) and "RUN --mount=" not in read(path),
            f"Updater-compatible legacy Docker build required: {path}")
for path in (ROOT / "tests").glob("*.py"):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([v.name for v in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        require(not any(v.split(".")[0] in {"app", "main", "fastapi", "sqlalchemy", "updater"}
                        for v in names), f"Test imports retired Python application: {path.name}")
    require(not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"spec_from_file_location", "exec_module"}
                    for node in ast.walk(tree)),
            f"Test dynamically loads Python application code: {path.name}")
fleet = read("internal/updater/native_matrix_real_test.go")
require('const fleetNodeCount = 5' in fleet and 'go-sqlite' in fleet and 'go-mysql' in fleet,
        "Acceptance requires five native nodes and both database combinations")
require('python-' not in fleet.lower() and 'referenceWeb' not in fleet,
        "Native fleet must not select a Python runtime")
from scripts.check_ci_budget import check_workflows
require(not check_workflows(ROOT / ".github/workflows"), "Hosted CI exceeds three-minute boundary")
from tests.test_repository_policy import RepositoryPolicyRegressionTests
result = unittest.TextTestRunner(verbosity=1).run(
    unittest.defaultTestLoader.loadTestsFromTestCase(RepositoryPolicyRegressionTests))
require(result.wasSuccessful(), "Repository policy regression failed")
print("Native release/deployment policy passed; reviewed provenance and three-minute CI enforced")
