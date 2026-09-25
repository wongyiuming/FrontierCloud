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
release_ui = read("static/js/release-admin.js")
maintenance_ui = read("static/js/maintenance-admin.js")
maintenance_gate = read("nginx/maintenance-gate.conf")
maintenance_page = read("nginx/maintenance.html")
workflow = read(".github/workflows/docker.yml")
compose = read("docker-compose.yaml")
web_dockerfile = read("Dockerfile")
updater_dockerfile = read("updater/Dockerfile")
nginx_dockerfile = read("nginx/Dockerfile")

require('RELEASE_BRANCH = "main"' in release, "Web release control must target main")
require('CI_BRANCH = "dev"' in release, "Production verification must reuse dev CI")
require('RELEASE_BRANCH = "main"' in updater, "Updater must target main")
require('BRANCH_URL = f"{REPOSITORY_API}/branches/{RELEASE_BRANCH}"' in release,
        "Web release control must verify the current main HEAD")
require('REPOSITORY_FULL_NAME = "wongyiuming/FrontierCloud"' in release,
        "Web release control must bind promotion provenance to this repository")
require('f"{REPOSITORY_API}/commits/{main_sha}/pulls"' in release and '_promotion_source_sha(pulls)' in release,
        "Web release control must resolve the reviewed PR associated with main HEAD")
require('base.get("ref") == RELEASE_BRANCH' in release and 'head.get("ref") == CI_BRANCH' in release
        and 'head_repo.get("full_name") == REPOSITORY_FULL_NAME' in release,
        "Web release control must require a merged same-repository dev-to-main PR")
require('"head_sha": source_sha' in release and '_matching_ci_run(runs, source_sha)' in release,
        "Web release control must query the exact reviewed dev PR head SHA")
require('source_tree != main_tree' in release,
        "Web release control must reject a main tree that differs from the reviewed dev PR tree")
require('item.get("head_sha")' in release and 'item.get("head_branch") == CI_BRANCH' in release,
        "Web release control must require the exact dev push CI")
require('origin/dev' not in updater, "Updater must not validate releases against dev")
require('refs/remotes/origin/{RELEASE_BRANCH}' in updater,
        "Updater must maintain an explicit origin/main remote-tracking ref")
require('refs/heads/{RELEASE_BRANCH}' in updater,
        "Updater fetch must not depend on a pre-existing remote fetch refspec")
require('FORCE_OPEN_FLAG = ROOT / "data" / ".frontiercloud-force-open"' in updater
        and 'FORCE_OPEN_FLAG.unlink(missing_ok=True)' in updater,
        "Every updater must clear stale open overrides before entering release maintenance")
require('followers_need_convergence' in release and 'cluster_convergence_needed' in release,
        "Web release control must allow retrying partial cluster convergence")
require('dev CI:' not in nodes and '${branch} CI:' in nodes,
        "Legacy node release renderer must still display the actual release branch")
require('systemVersionPanel' in release_ui and '系统版本管理' in release_ui,
        "Production release controls must have a standalone Admin module")
require('schedule(masterBusy ? 1500 : 5000)' in release_ui,
        "Release Admin must poll rapidly while a release is active")
require('siteAccessPanel' in maintenance_ui and '站点开放状态' in maintenance_ui,
        "Site availability must have a standalone Admin module")
require('.frontiercloud-maintenance' in maintenance_gate and '.frontiercloud-force-open' in maintenance_gate,
        "Nginx must combine release and explicit Admin maintenance gates")
require('前沿娱乐 · 系统维护' in maintenance_page and '立即重试' in maintenance_page,
        "Public maintenance page must use the branded maintenance UI")
require('branches: ["dev", "main"]' in workflow,
        "Workflow must retain a lightweight main migration/promotion run")
require('promote-main:' in workflow and "github.ref == 'refs/heads/main'" in workflow,
        "Main push must use the lightweight promotion gate")
require("github.ref == 'refs/heads/dev'" in workflow,
        "Full CI must run on dev pushes")
require('actions/github-script@v7' in workflow and 'listPullRequestsAssociatedWithCommit' in workflow,
        "Main promotion must resolve the reviewed PR associated with main HEAD")
require("pr?.base?.ref === 'main'" in workflow and "pr?.head?.ref === 'dev'" in workflow
        and 'pr?.head?.repo?.full_name === `${owner}/${repo}`' in workflow,
        "Main promotion must require a merged same-repository dev-to-main PR")
require('sourceTree !== mainTree' in workflow,
        "Main promotion must verify that the reviewed dev PR tree equals the main tree")
require('head_sha: sourceSha' in workflow and 'item?.head_sha === sourceSha' in workflow,
        "Main promotion must query the exact reviewed dev PR head SHA")
require('mainCommit.data.parents' not in workflow and 'parents[1]' not in release,
        "Production promotion must not depend on the GitHub merge method")
require('verify-promotion-query:' in workflow and 'head_sha: context.sha' in workflow,
        "Dev CI must verify that the exact-SHA promotion lookup works before merge")
require('github.paginate' not in workflow,
        "Main promotion must not rely on historical workflow pagination")
require("github.event_name != 'pull_request' || github.head_ref != 'dev'" not in workflow,
        "Full CI must not rerun automatically on main")

# Keep release runtime foundations on explicit patch versions. Digest pinning can
# be layered on later, but broad minor/major tags must not silently move beneath
# an unchanged FrontierCloud commit.
require('FROM python:3.14.7-slim' in web_dockerfile,
        "Web Python base image must be patch-pinned")
require('FROM python:3.14.7-alpine' in updater_dockerfile,
        "Updater Python base image must be patch-pinned")
require('FROM nginx:1.30.4-alpine' in nginx_dockerfile,
        "Nginx base image must be patch-pinned")
require('image: redis:7.4.11-alpine' in compose,
        "Redis image must be patch-pinned")
require('image: mysql:8.4.11' in compose,
        "MySQL image must be patch-pinned")
require('image: coturn/coturn:4.17.2-r0-alpine' in compose,
        "Coturn image must remain patch-pinned")
require('image: redis:7-alpine' not in compose,
        "Broad Redis major tag must not return")

# The updater must retain write access to its socket volume, while the Web
# process only needs to connect to the existing Unix socket and must not mutate
# the control-volume filesystem itself.
updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
web_block = compose.split("  web:\n", 1)[1].split("\n  redis:\n", 1)[0]
require('updater_control:/run/frontiercloud-updater\n' in updater_block,
        "Updater must retain writable control-volume access")
require('updater_control:/run/frontiercloud-updater:ro' not in updater_block,
        "Updater control volume cannot be read-only")
require('updater_control:/run/frontiercloud-updater:ro' in web_block,
        "Web must mount the updater control volume read-only")

for path in ("Dockerfile", "nginx/Dockerfile", "updater/Dockerfile"):
    source = read(path)
    require("COPY --chmod=" not in source, f"{path} requires BuildKit COPY --chmod")
    require("RUN --mount=" not in source, f"{path} requires BuildKit RUN --mount")

print("Release policy contract passed: reviewed dev provenance and P1 runtime hardening are enforced")
