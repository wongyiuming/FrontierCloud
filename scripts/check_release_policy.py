#!/usr/bin/env python3
"""Fail CI if production release controls drift away from main-only policy."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


release = read("app/services/release_control.py")
updater = read("updater/server.py")
nodes = read("static/js/nodes.js")
workflow = read(".github/workflows/docker.yml")

require('RELEASE_BRANCH = "main"' in release, "Web release control must target main")
require('RELEASE_BRANCH = "main"' in updater, "Updater must target main")
require('BRANCH_URL = f"{REPOSITORY_API}/branches/{RELEASE_BRANCH}"' in release,
        "Web release control must verify the current main HEAD")
require('"branch": RELEASE_BRANCH' in release and '"event": "push"' in release,
        "Web release control must verify a main push CI run")
require('origin/dev' not in updater, "Updater must not validate releases against dev")
require('origin/{RELEASE_BRANCH}' in updater, "Updater must validate against origin/main")
require('dev CI:' not in nodes and '${branch} CI:' in nodes,
        "Admin release UI must display the actual release branch")
require('branches: ["dev", "main"]' in workflow,
        "CI must run again after a PR is merged into main")

for path in ("Dockerfile", "nginx/Dockerfile", "updater/Dockerfile"):
    source = read(path)
    require("COPY --chmod=" not in source, f"{path} requires BuildKit COPY --chmod")
    require("RUN --mount=" not in source, f"{path} requires BuildKit RUN --mount")

print("Release policy contract passed: production follows tested main only")
