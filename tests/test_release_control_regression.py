"""Round-two regression coverage for Web-managed releases and cluster convergence."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api import internal_cluster_update
from app.services import cluster_update_coordinator as coordinator
from app.services import release_control


MAIN_SHA = "a" * 40
SOURCE_SHA = "b" * 40
TREE_SHA = "c" * 40
OLD_SHA = "d" * 40
PREVIOUS_SHA = "e" * 40


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeClient:
    def __init__(self, *, branch: dict, runs: dict | None = None, source: dict | None = None,
                 pulls: list[dict] | None = None):
        self.branch = branch
        self.runs = runs or {"workflow_runs": []}
        self.source = source or {}
        self.pulls = [promotion_pr()] if pulls is None else pulls
        self.requests: list[tuple[str, dict | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, params: dict | None = None) -> FakeResponse:
        self.requests.append((url, params))
        if url == release_control.BRANCH_URL:
            return FakeResponse(self.branch)
        if url == release_control.CI_URL:
            return FakeResponse(self.runs)
        if url == f"{release_control.REPOSITORY_API}/commits/{MAIN_SHA}/pulls":
            return FakeResponse(self.pulls)
        if url == f"{release_control.REPOSITORY_API}/commits/{SOURCE_SHA}":
            return FakeResponse(self.source)
        raise AssertionError(f"unexpected GitHub request: {url}")


def branch_payload(*, tree: str = TREE_SHA) -> dict:
    return {
        "commit": {
            "sha": MAIN_SHA,
            "commit": {"tree": {"sha": tree}},
            "parents": [{"sha": OLD_SHA}],
        }
    }


def promotion_pr(*, source: str = SOURCE_SHA, base: str = "main", head: str = "dev",
                 repo: str = "wongyiuming/FrontierCloud", merged_at: str | None = "2026-09-25T00:00:00Z") -> dict:
    return {
        "merged_at": merged_at,
        "base": {"ref": base},
        "head": {"ref": head, "sha": source, "repo": {"full_name": repo}},
    }


def source_payload(*, tree: str = TREE_SHA) -> dict:
    return {"commit": {"tree": {"sha": tree}}}


def dev_run(*, status: str = "completed", conclusion: str | None = "success", sha: str = SOURCE_SHA) -> dict:
    return {
        "head_branch": "dev",
        "event": "push",
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "run_number": 999,
        "html_url": "https://example.invalid/run/999",
        "updated_at": "2026-09-25T00:00:00Z",
    }


class ReleaseCiRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        release_control._ci_cache = (0.0, {})

    def tearDown(self):
        release_control._ci_cache = (0.0, {})

    async def run_ci(self, client: FakeClient) -> dict:
        with patch.object(release_control.httpx, "AsyncClient", return_value=client):
            return await release_control.ci_status(force=True)

    async def test_publishable_requires_reviewed_dev_pr_exact_push_and_same_tree(self):
        client = FakeClient(
            branch=branch_payload(),
            source=source_payload(),
            runs={"workflow_runs": [
                {**dev_run(), "head_branch": "main"},
                {**dev_run(), "event": "workflow_dispatch"},
                dev_run(),
            ]},
        )
        result = await self.run_ci(client)
        self.assertTrue(result["publishable"])
        self.assertEqual(result["sha"], MAIN_SHA)
        self.assertEqual(result["ci_sha"], SOURCE_SHA)
        self.assertEqual(result["tree_sha"], TREE_SHA)
        ci_request = next(item for item in client.requests if item[0] == release_control.CI_URL)
        self.assertEqual(ci_request[1]["event"], "push")
        self.assertEqual(ci_request[1]["head_sha"], SOURCE_SHA)

    async def test_publishable_rejects_missing_failed_or_tree_mismatched_dev_ci(self):
        cases = (
            ([], source_payload(), "no matching dev push CI"),
            ([dev_run(conclusion="failure")], source_payload(), "failed"),
            ([dev_run()], source_payload(tree="f" * 40), "differs"),
        )
        for runs, source, detail in cases:
            with self.subTest(detail=detail):
                release_control._ci_cache = (0.0, {})
                result = await self.run_ci(FakeClient(
                    branch=branch_payload(),
                    runs={"workflow_runs": runs},
                    source=source,
                ))
                self.assertFalse(result["publishable"])
                self.assertIn(detail, result["detail"])

    async def test_main_without_unique_reviewed_dev_pr_is_never_publishable(self):
        result = await self.run_ci(FakeClient(branch=branch_payload(), pulls=[]))
        self.assertFalse(result["publishable"])
        self.assertIn("no unique merged dev->main PR association", result["detail"])

    async def test_merge_method_is_irrelevant_but_pr_provenance_is_strict(self):
        for pulls in (
            [promotion_pr(head="feature")],
            [promotion_pr(repo="example/fork")],
            [promotion_pr(merged_at=None)],
            [promotion_pr(), promotion_pr(source="f" * 40)],
        ):
            with self.subTest(pulls=pulls):
                release_control._ci_cache = (0.0, {})
                result = await self.run_ci(FakeClient(branch=branch_payload(), pulls=pulls))
                self.assertFalse(result["publishable"])
                self.assertIn("no unique merged dev->main PR association", result["detail"])


class ReleasePolicyRegressionTests(unittest.IsolatedAsyncioTestCase):
    def follower(self, *, sha: str = MAIN_SHA, state: str = "success", branch: str = "main", reachable: bool = True):
        return {
            "peer_id": "follower-1",
            "peer_endpoint": "https://follower.invalid",
            "reachable": reachable,
            "status": {"release_branch": branch, "current_sha": sha, "state": state},
        }

    async def test_release_status_only_enables_upgrade_for_real_cluster_drift(self):
        local = {
            "release_branch": "main", "current_sha": MAIN_SHA, "previous_sha": PREVIOUS_SHA,
            "state": "idle", "phase": "idle",
        }
        ci = {"sha": MAIN_SHA, "publishable": True}
        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "agent_status", AsyncMock(return_value=local)),
            patch.object(release_control, "ci_status", AsyncMock(return_value=ci)),
            patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[self.follower()])),
        ):
            converged = await release_control.release_status()
        self.assertFalse(converged["can_upgrade"])
        self.assertTrue(converged["can_rollback"])
        self.assertFalse(converged["cluster_convergence_needed"])

        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "agent_status", AsyncMock(return_value=local)),
            patch.object(release_control, "ci_status", AsyncMock(return_value=ci)),
            patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[self.follower(sha=OLD_SHA)])),
        ):
            drifted = await release_control.release_status()
        self.assertTrue(drifted["can_upgrade"])
        self.assertTrue(drifted["cluster_convergence_needed"])

    async def test_release_policy_blocks_legacy_or_unreachable_updaters(self):
        local = {"release_branch": "main", "current_sha": OLD_SHA, "previous_sha": PREVIOUS_SHA, "state": "idle"}
        ci = {"sha": MAIN_SHA, "publishable": True}
        for follower in (
            self.follower(branch="dev"),
            self.follower(reachable=False),
        ):
            with (
                patch.object(release_control.state, "node", {"role": "Master"}),
                patch.object(release_control, "agent_status", AsyncMock(return_value=local)),
                patch.object(release_control, "ci_status", AsyncMock(return_value=ci)),
                patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[follower])),
            ):
                value = await release_control.release_status()
            self.assertFalse(value["release_policy_ready"])
            self.assertFalse(value["can_upgrade"])
            self.assertFalse(value["can_rollback"])

    async def test_start_upgrade_sends_exact_main_target_and_holds_master_maintenance(self):
        request = AsyncMock(return_value={"ok": True, "accepted": True})
        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "ci_status", AsyncMock(return_value={"sha": MAIN_SHA, "publishable": True})),
            patch.object(release_control, "agent_status", AsyncMock(return_value={"release_branch": "main", "current_sha": OLD_SHA})),
            patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[self.follower(sha=OLD_SHA)])),
            patch.object(release_control, "agent_request", request),
        ):
            result = await release_control.start_upgrade()
        self.assertTrue(result["accepted"])
        request.assert_awaited_once_with({
            "action": "start", "target_sha": MAIN_SHA, "mode": "upgrade", "hold_maintenance": True,
        })

    async def test_start_upgrade_rejects_unpublishable_or_already_converged_target(self):
        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "ci_status", AsyncMock(return_value={"sha": MAIN_SHA, "publishable": False})),
        ):
            with self.assertRaisesRegex(RuntimeError, "Current main HEAD"):
                await release_control.start_upgrade()

        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "ci_status", AsyncMock(return_value={"sha": MAIN_SHA, "publishable": True})),
            patch.object(release_control, "agent_status", AsyncMock(return_value={"release_branch": "main", "current_sha": MAIN_SHA})),
            patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[self.follower()])),
        ):
            with self.assertRaisesRegex(RuntimeError, "already converged"):
                await release_control.start_upgrade()

    async def test_start_rollback_uses_previous_web_managed_sha_and_holds_maintenance(self):
        request = AsyncMock(return_value={"ok": True, "accepted": True})
        with (
            patch.object(release_control.state, "node", {"role": "Master"}),
            patch.object(release_control, "agent_status", AsyncMock(return_value={
                "release_branch": "main", "current_sha": MAIN_SHA, "previous_sha": PREVIOUS_SHA,
            })),
            patch.object(release_control, "follower_release_statuses", AsyncMock(return_value=[self.follower()])),
            patch.object(release_control, "agent_request", request),
        ):
            result = await release_control.start_rollback()
        self.assertTrue(result["accepted"])
        request.assert_awaited_once_with({
            "action": "start", "target_sha": PREVIOUS_SHA, "mode": "rollback", "hold_maintenance": True,
        })


class FollowerReleaseApiRegressionTests(unittest.IsolatedAsyncioTestCase):
    def request(self, payload: dict):
        return SimpleNamespace(state=SimpleNamespace(node_control_body=json.dumps(payload).encode()))

    async def test_only_upstream_master_can_control_follower_release(self):
        with patch.object(internal_cluster_update.state, "node", {"role": "Follower"}):
            internal_cluster_update.require_master_relation({"direction": "upstream"})
            with self.assertRaises(HTTPException) as caught:
                internal_cluster_update.require_master_relation({"direction": "downstream"})
        self.assertEqual(caught.exception.status_code, 403)

        with patch.object(internal_cluster_update.state, "node", {"role": "Master"}):
            with self.assertRaises(HTTPException) as caught:
                internal_cluster_update.require_master_relation({"direction": "upstream"})
        self.assertEqual(caught.exception.status_code, 403)

    async def test_follower_start_forwards_release_without_holding_cluster_maintenance(self):
        request = AsyncMock(return_value={"ok": True})
        with (
            patch.object(internal_cluster_update.state, "node", {"role": "Follower"}),
            patch.object(internal_cluster_update, "authenticated", AsyncMock(return_value={"direction": "upstream"})),
            patch.object(internal_cluster_update, "agent_request", request),
        ):
            value = await internal_cluster_update.start(self.request({"target_sha": MAIN_SHA, "mode": "upgrade"}))
        self.assertTrue(value["accepted"])
        request.assert_awaited_once_with({
            "action": "start", "target_sha": MAIN_SHA, "mode": "upgrade", "hold_maintenance": False,
        })

    async def test_duplicate_same_target_follower_start_is_idempotently_accepted(self):
        response = {"ok": False, "reason": "release already running", "status": {
            "target_sha": MAIN_SHA, "state": "running",
        }}
        with (
            patch.object(internal_cluster_update.state, "node", {"role": "Follower"}),
            patch.object(internal_cluster_update, "authenticated", AsyncMock(return_value={"direction": "upstream"})),
            patch.object(internal_cluster_update, "agent_request", AsyncMock(return_value=response)),
        ):
            value = await internal_cluster_update.start(self.request({"target_sha": MAIN_SHA, "mode": "upgrade"}))
        self.assertTrue(value["accepted"])
        self.assertEqual(value["status"]["state"], "running")


class ClusterCoordinatorRegressionTests(unittest.IsolatedAsyncioTestCase):
    def relation(self, peer: str):
        return {
            "relationship_id": f"rel-{peer}", "peer_id": peer,
            "peer_endpoint": f"https://{peer}.invalid", "direction": "downstream", "state": "active",
        }

    async def test_command_failure_aborts_cluster_release_and_closes_resources(self):
        relations = [self.relation("f1"), self.relation("f2")]

        async def call(relation, path, payload):
            if path.endswith("/start") and relation["peer_id"] == "f2":
                raise RuntimeError("offline")
            return {"accepted": True}

        close_db = AsyncMock()
        close_transport = AsyncMock()
        with (
            patch.object(coordinator, "init_db", AsyncMock()),
            patch.object(coordinator.state, "initialize", AsyncMock()),
            patch.object(coordinator.state, "node", {"role": "Master"}),
            patch.object(coordinator.state, "list_relationships", AsyncMock(return_value=relations)),
            patch.object(coordinator.runtime, "call", AsyncMock(side_effect=call)),
            patch.object(coordinator.transport, "open", MagicMock()),
            patch.object(coordinator.transport, "close", close_transport),
            patch.object(coordinator, "close_db", close_db),
        ):
            with self.assertRaisesRegex(RuntimeError, "release command failed for: f2"):
                await coordinator.run(MAIN_SHA, "upgrade")
        close_transport.assert_awaited_once()
        close_db.assert_awaited_once()

    async def test_follower_failure_is_not_misreported_as_convergence(self):
        relations = [self.relation("f1"), self.relation("f2")]

        async def call(relation, path, payload):
            if path.endswith("/start"):
                return {"accepted": True}
            if relation["peer_id"] == "f2":
                return {"status": {"target_sha": MAIN_SHA, "current_sha": OLD_SHA, "state": "failed", "detail": "build failed"}}
            return {"status": {"target_sha": MAIN_SHA, "current_sha": MAIN_SHA, "state": "success"}}

        with (
            patch.object(coordinator, "init_db", AsyncMock()),
            patch.object(coordinator.state, "initialize", AsyncMock()),
            patch.object(coordinator.state, "node", {"role": "Master"}),
            patch.object(coordinator.state, "list_relationships", AsyncMock(return_value=relations)),
            patch.object(coordinator.runtime, "call", AsyncMock(side_effect=call)),
            patch.object(coordinator.transport, "open", MagicMock()),
            patch.object(coordinator.transport, "close", AsyncMock()),
            patch.object(coordinator, "close_db", AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Follower release failed: f2:build failed"):
                await coordinator.run(MAIN_SHA, "upgrade")

    async def test_coordinator_waits_until_every_active_follower_reaches_exact_target(self):
        relations = [self.relation("f1"), self.relation("f2")]
        probes = {"f2": 0}

        async def call(relation, path, payload):
            if path.endswith("/start"):
                self.assertEqual(payload, {"target_sha": MAIN_SHA, "mode": "upgrade"})
                return {"accepted": True}
            if relation["peer_id"] == "f1":
                return {"status": {"target_sha": MAIN_SHA, "current_sha": MAIN_SHA, "state": "success"}}
            probes["f2"] += 1
            if probes["f2"] == 1:
                return {"status": {"target_sha": MAIN_SHA, "current_sha": OLD_SHA, "state": "running"}}
            return {"status": {"target_sha": MAIN_SHA, "current_sha": MAIN_SHA, "state": "success"}}

        sleeper = AsyncMock()
        with (
            patch.object(coordinator, "init_db", AsyncMock()),
            patch.object(coordinator.state, "initialize", AsyncMock()),
            patch.object(coordinator.state, "node", {"role": "Master"}),
            patch.object(coordinator.state, "list_relationships", AsyncMock(return_value=relations)),
            patch.object(coordinator.runtime, "call", AsyncMock(side_effect=call)),
            patch.object(coordinator.transport, "open", MagicMock()),
            patch.object(coordinator.transport, "close", AsyncMock()),
            patch.object(coordinator, "close_db", AsyncMock()),
            patch.object(coordinator.asyncio, "sleep", sleeper),
        ):
            await coordinator.run(MAIN_SHA, "upgrade")
        self.assertEqual(probes["f2"], 2)
        sleeper.assert_awaited_once_with(coordinator.POLL_SECONDS)


if __name__ == "__main__":
    unittest.main()
