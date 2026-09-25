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

require('RELEASE_BRANCH = "main"' in release, "Web release control must target main")
require('CI_BRANCH = "dev"' in release, "Production verification must reuse dev CI")
require('RELEASE_BRANCH = "main"' in updater, "Updater must target main")
require('BRANCH_URL = f"{REPOSITORY_API}/branches/{RELEASE_BRANCH}"' in release,
        "Web release control must verify the current main HEAD")
require('"branch": CI_BRANCH' in release and '"event": "push"' in release,
        "Web release control must verify dev push CI")
require('tree_id' in release and 'tree_sha' in release and '_matching_ci_run' in release,
        "Production publishability must bind main tree to a tested dev tree")
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
require('actions/github-script@v7' in workflow and 'mainCommit.data.parents' in workflow,
        "Main promotion must derive the exact merged dev commit")
require('parents[1]?.sha' in workflow and 'sourceTree !== mainTree' in workflow,
        "Main promotion must verify that the merged dev tree equals the main tree")
require('head_sha: sourceSha' in workflow and 'item?.head_sha === sourceSha' in workflow,
        "Main promotion must query the exact merged dev SHA instead of scanning historical trees")
require('verify-promotion-query:' in workflow and 'head_sha: context.sha' in workflow,
        "Dev CI must verify that the exact-SHA promotion lookup works before merge")
require('github.paginate' not in workflow,
        "Main promotion must not rely on historical workflow pagination")
require("github.event_name != 'pull_request' || github.head_ref != 'dev'" not in workflow,
        "Full CI must not rerun automatically on main")

for path in ("Dockerfile", "nginx/Dockerfile", "updater/Dockerfile"):
    source = read(path)
    require("COPY --chmod=" not in source, f"{path} requires BuildKit COPY --chmod")
    require("RUN --mount=" not in source, f"{path} requires BuildKit RUN --mount")

print("Release policy contract passed: main promotes an already-tested dev commit")
