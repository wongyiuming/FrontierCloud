"""Whole-manifest queue/RPC/convergence boundaries for the reference runtime."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import httpx

from app.api import internal_cluster_update as api
from app.services import cluster_manifest_coordinator as coordinator
from app.services import release_manifest_control as master
from app.services.federation import protocol as p
from app.services.federation import release_manifest as manifests
from app.services.federation.transport import ControlHTTPError, Transport
from tests.test_updater_p0_contract import load_updater_module
from updater import release_evidence


def fixture():
    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "protocol/v2/vectors/release-manifest.json").read_text(encoding="utf-8"))["cases"][0]["manifest"]


class ManifestQueueTests(unittest.TestCase):
    def test_queue_selects_private_main_artifact_and_publishes_whole_digest(self):
        updater = load_updater_module()
        manifest = fixture()
        status = {"state": "idle", "current_sha": "9" * 40}
        tasks = MagicMock()
        tasks.full.return_value = False
        handler = updater.Handler.__new__(updater.Handler)
        handler.rfile = io.BytesIO(p.canonical({"action": "start", "release_manifest": manifest, "mode": "upgrade"}) + b"\n")
        handler.wfile = io.BytesIO()
        with (patch.object(updater, "TASKS", tasks), patch.object(updater, "read_status", side_effect=lambda: copy.deepcopy(status)),
              patch.object(updater, "write_status", side_effect=lambda **changes: status.update(changes))):
            handler.handle()
        reply = json.loads(handler.wfile.getvalue())
        self.assertEqual(reply["target_sha"], "a" * 40)
        self.assertEqual(reply["release_id"], manifests.identifier(manifest))
        self.assertEqual(tasks.put_nowait.call_args.args[0], ("a" * 40, "upgrade", False, manifest))
        self.assertEqual(status["target_manifest"], manifest)

    def test_manifest_history_advances_even_when_local_sha_does_not(self):
        updater = load_updater_module()
        old, new = fixture(), fixture()
        new["release_version"] = "2.0.0rc2"
        current = {"current_manifest": old, "current_sha": "a" * 40}
        changes = updater.manifest_changes(current, new, "a" * 40, "upgrade")
        self.assertEqual(changes["previous_manifest"], old)
        self.assertEqual(changes["current_manifest"], new)
        current.update(changes)
        self.assertEqual(updater.manifest_changes(current, new, "a" * 40, "upgrade"), {})
        changes = updater.manifest_changes(current, old, "a" * 40, "rollback")
        self.assertIsNone(changes["previous_manifest"])
        self.assertEqual(changes["current_manifest"], old)
        with self.assertRaises(ValueError):
            updater.validate_manifest_state({"current_manifest": new, "current_sha": "d" * 40})

    def test_failed_independent_ci_proof_never_reaches_filesystem_source_or_docker(self):
        updater = load_updater_module()
        with (patch.object(updater, "read_status", return_value={"current_sha": "9" * 40}),
              patch.object(updater, "write_status") as persist, patch.object(updater, "verify_artifact", side_effect=ValueError("CI failed")),
              patch.object(updater, "FORCE_OPEN_FLAG") as override, patch.object(updater, "maintenance") as fence,
              patch.object(updater, "validate_target") as source, patch.object(updater, "client") as docker):
            updater.perform("a" * 40, "upgrade", True, fixture())
        override.unlink.assert_not_called()
        fence.assert_not_called()
        source.assert_not_called()
        docker.assert_not_called()
        self.assertEqual(persist.call_args.kwargs["state"], "failed")

    def test_exact_historical_proof_newest_failed_run_and_wrong_pr_are_rejected(self):
        manifest = fixture()
        paths = []
        failed, wrong_pr = False, False

        def get(path):
            paths.append(path)
            if path.endswith("/pulls?per_page=100"):
                return [{"merged_at": "now", "merge_commit_sha": "8" * 40 if wrong_pr else "a" * 40,
                         "base": {"ref": "main"}, "head": {"ref": "dev", "sha": "b" * 40,
                         "repo": {"full_name": release_evidence.REPOSITORY}}}]
            if path.startswith("/commits/"):
                return {"sha": path.rsplit("/", 1)[1], "commit": {"tree": {"sha": "c" * 40}}}
            runs = [{"run_number": 1, "head_branch": "dev", "head_sha": "b" * 40, "event": "push", "status": "completed", "conclusion": "success"}]
            if failed:
                runs.append({**runs[0], "run_number": 2, "conclusion": "failure"})
            return {"workflow_runs": runs}

        self.assertTrue(release_evidence.verify_artifact(manifest, get=get)["publishable"])
        self.assertFalse(any("/branches/" in path for path in paths))
        failed = True
        with self.assertRaises(p.ProtocolError):
            release_evidence.verify_artifact(manifest, get=get)
        failed, wrong_pr = False, True
        with self.assertRaises(ValueError):
            release_evidence.verify_artifact(manifest, get=get)


class ManifestApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_socket_gap_is_http503_not_successful_capability_loss(self):
        with (patch.object(api.state, "node", {"role": "Follower"}),
              patch.object(api, "authenticated", AsyncMock(return_value={"direction": "upstream"})),
              patch.object(api, "agent_request", AsyncMock(return_value={"ok": False, "reason": "private socket failure"}))):
            with self.assertRaises(HTTPException) as caught:
                await api.status(SimpleNamespace())
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn("private", caught.exception.detail)
        with (patch.object(api.state, "node", {"role": "Follower"}),
              patch.object(api, "authenticated", AsyncMock(return_value={"direction": "upstream"})),
              patch.object(api, "agent_request", AsyncMock(return_value={"ok": True, "status": {"release_branch": "main", "state": "success"}, "capabilities": [manifests.MANIFEST_CAPABILITY]}))):
            value = await api.status(SimpleNamespace())
            self.assertEqual(value["status"]["release_branch"], "main")
            self.assertEqual(value["capabilities"], [manifests.MANIFEST_CAPABILITY])

    async def test_whole_manifest_forwarding_idempotence_and_ambiguous_targets(self):
        manifest = fixture()
        release_id = manifests.identifier(manifest)
        def request(value):
            return SimpleNamespace(state=SimpleNamespace(node_control_body=p.canonical(value)))
        agent = AsyncMock(return_value={"ok": True, "release_id": release_id})
        with (patch.object(api.state, "node", {"role": "Follower"}),
              patch.object(api, "authenticated", AsyncMock(return_value={"direction": "upstream"})),
              patch.object(api, "agent_request", agent)):
            reply = await api.start(request({"release_manifest": manifest, "mode": "upgrade"}))
            self.assertEqual(reply["release_id"], release_id)
            self.assertNotIn("target_sha", agent.call_args.args[0])
            self.assertEqual(agent.call_args.args[0]["release_manifest"], manifest)
            agent.return_value = {"ok": False, "status": {"target_manifest": manifest, "mode": "upgrade", "state": "running"}}
            self.assertEqual((await api.start(request({"release_manifest": manifest, "mode": "upgrade"})))["release_id"], release_id)
            for bad in ({"release_manifest": manifest, "target_sha": "a" * 40}, {"release_manifest": None}, {"release_manifest": manifest, "mode": "rollback"}):
                with self.assertRaises(HTTPException):
                    await api.start(request(bad))


class ManifestConvergenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_control_http_errors_preserve_only_status_not_upstream_body(self):
        for code in (401,403,502,503,504):
            transport=Transport()
            transport.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(code,text="never-propagate-upstream-body")))
            try:
                with patch.object(transport,"open"),self.assertRaises(ControlHTTPError) as caught:
                    await transport.request("https://peer.invalid","/internal/v1/cluster-update/status")
                self.assertEqual(caught.exception.status_code,code)
                self.assertEqual(str(caught.exception),f"Node control HTTP {code}")
            finally:
                await transport.close()

    async def test_mixed_private_profiles_whole_digest_and_all_peer_preflight(self):
        for fault in ("", "old", "wrong-sha", "wrong-manifest", "unacknowledged", "reset", "revoked", "failed", "http502", "http503", "http504", "http401", "http403", "invalid", "http-preflight", "http-outage"):
            with self.subTest(fault=fault):
                manifest = fixture()
                release_id = manifests.identifier(manifest)
                peers = [{"relationship_id": str(index), "peer_id": str(index), "peer_endpoint": "https://peer.invalid",
                          "peer_key": "pin", "credential": "encrypted", "direction": "downstream", "state": "active", "protocol": 2} for index in range(9)]
                identity = {"node_id": "node", "role": "Master", "private_key": "sealed"}
                starts = []
                probes = set()
                succeeds = fault in {"", "http502", "http503", "http504"}

                async def call(peer, path, payload):
                    index = int(peer["relationship_id"])
                    branch = "main" if index % 2 else "gin_main"
                    if path.endswith("/start"):
                        self.assertEqual(payload, {"release_manifest": manifest, "mode": "upgrade"})
                        starts.append(index)
                        return {"accepted": True, "release_id": "wrong" if fault == "unacknowledged" else release_id}
                    if fault == "http-preflight": raise ControlHTTPError(503)
                    if starts and fault == "http-outage": raise ControlHTTPError(503)
                    if starts and fault == "invalid": raise p.ProtocolError("Invalid node control response")
                    if starts and fault.startswith("http") and index not in probes:
                        probes.add(index)
                        raise ControlHTTPError(int(fault[4:]))
                    current = copy.deepcopy(manifest)
                    if starts and fault == "wrong-manifest": current["release_version"] = "2.0.0rc9"
                    if fault == "reset": identity["role"] = "Standalone"
                    if fault == "revoked": peers[0]["state"] = "revoked"
                    return {"capabilities": [] if fault == "old" else [manifests.MANIFEST_CAPABILITY], "status": {
                        "release_branch": branch, "current_sha": "9" * 40 if starts and fault == "wrong-sha" else manifest["artifacts"][branch]["commit_sha"],
                        "current_manifest": current, "target_manifest": manifest, "state": "failed" if fault == "failed" else "success"}}

                with (patch.object(coordinator.state, "read_existing_identity", AsyncMock(side_effect=lambda: copy.deepcopy(identity))),
                      patch.object(coordinator.state, "list_relationships", AsyncMock(side_effect=lambda: copy.deepcopy(peers))),
                      patch.object(coordinator.state, "relationship", AsyncMock(side_effect=lambda key: copy.deepcopy(peers[int(key)]))),
                      patch.object(coordinator.runtime, "call", AsyncMock(side_effect=call)), patch.object(coordinator, "TIMEOUT_SECONDS", 1 if succeeds else 0.2),
                      patch.object(coordinator, "POLL_SECONDS", 0.001)):
                    if not succeeds:
                        with self.assertRaises((p.ProtocolError, TimeoutError)):
                            await coordinator.converge(manifest, "upgrade")
                    else:
                        await coordinator.converge(manifest, "upgrade")
                if fault in {"old", "reset", "revoked", "http-preflight"}: self.assertEqual(starts, [])
                if succeeds: self.assertEqual(len(starts), 9)
                if fault in {"http502", "http503", "http504"}: self.assertEqual(len(probes), 9)


class MasterManifestTests(unittest.IsolatedAsyncioTestCase):
    def test_reference_legacy_ci_also_cannot_reuse_older_success(self):
        from app.services import release_control
        successful = {"run_number": 1, "head_branch": "dev", "head_sha": "a" * 40, "event": "push", "status": "completed", "conclusion": "success"}
        failed = {**successful, "run_number": 2, "conclusion": "failure"}
        self.assertEqual(release_control._matching_ci_run([successful, failed], "a" * 40), failed)

    async def test_published_source_only_exact_joint_proof_history_and_fresh_membership(self):
        for fault in ("", "rollback", "old-peer", "failed-ci", "revoked", "reset", "unacknowledged", "sha-history-only"):
            with self.subTest(fault=fault):
                manifest = fixture()
                release_id = manifests.identifier(manifest)
                identity = {"node_id": "master", "role": "Master", "private_key": "sealed"}
                peers = [{"relationship_id": "peer", "peer_id": "peer", "peer_endpoint": "https://peer.invalid", "peer_key": "pin", "credential": "sealed", "direction": "downstream", "state": "active", "protocol": 2}]
                local = {"status": {"release_branch": "main", "state": "success", "current_sha": "9" * 40, "previous_sha": "8" * 40,
                                    "previous_manifest": manifest}, "capabilities": [manifests.MANIFEST_CAPABILITY]}
                followers = [{"reachable": True, "status": {"release_branch": "gin_main", "state": "success", "current_sha": "d" * 40},
                              "capabilities": [] if fault == "old-peer" else [manifests.MANIFEST_CAPABILITY]}]
                if fault == "sha-history-only": local["status"].pop("previous_manifest")

                def verify(value, mode):
                    self.assertEqual(value, manifest)
                    if fault == "failed-ci": raise p.ProtocolError("CI failed")
                    if fault == "revoked": peers[0]["state"] = "revoked"
                    if fault == "reset": identity["role"] = "Standalone"

                agent = AsyncMock(return_value={"ok": True, "release_id": "wrong" if fault == "unacknowledged" else release_id})
                source = MagicMock(return_value=manifest)
                with (patch.object(master.legacy.state, "read_existing_identity", AsyncMock(side_effect=lambda: copy.deepcopy(identity))),
                      patch.object(master.legacy.state, "list_relationships", AsyncMock(side_effect=lambda: copy.deepcopy(peers))),
                      patch.object(master, "local_status", AsyncMock(return_value=local)),
                      patch.object(master.legacy, "follower_release_statuses", AsyncMock(return_value=followers)),
                      patch.object(master, "read_published", source), patch.object(master, "verify_joint", side_effect=verify),
                      patch.object(master.legacy, "agent_request", agent)):
                    mode = "rollback" if fault in {"rollback", "sha-history-only"} else "upgrade"
                    if fault in {"", "rollback"}:
                        self.assertEqual((await master.start(mode))["release_id"], release_id)
                        self.assertEqual(agent.call_args.args[0], {"action": "start", "release_manifest": manifest, "mode": mode, "hold_maintenance": True})
                    else:
                        with self.assertRaises((ValueError, p.ProtocolError)):
                            await master.start(mode)
                    if mode == "rollback": source.assert_not_called()
                    if fault not in {"", "rollback", "unacknowledged"}: agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
