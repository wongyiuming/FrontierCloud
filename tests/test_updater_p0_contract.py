"""P0 contracts for updater runtime continuity and bounded release images."""
from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
CURRENT_SHA = "a" * 40
PREVIOUS_SHA = "b" * 40
STALE_SHA = "c" * 40
RUNTIME_SHA = "d" * 40


def load_updater_module():
    fake_docker = types.ModuleType("docker")
    fake_docker.DockerClient = lambda *args, **kwargs: None
    previous = sys.modules.get("docker")
    sys.modules["docker"] = fake_docker
    try:
        spec = importlib.util.spec_from_file_location(
            "frontiercloud_updater_p0", ROOT / "updater/server.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("cannot load updater/server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("docker", None)
        else:
            sys.modules["docker"] = previous


class ReleaseImageRetentionTests(unittest.TestCase):
    def test_cleanup_keeps_only_current_previous_and_non_release_images(self):
        updater = load_updater_module()
        images = [
            SimpleNamespace(id="current", tags=[f"frontiercloud-web:{CURRENT_SHA}"]),
            SimpleNamespace(id="previous", tags=[f"frontiercloud-nginx:{PREVIOUS_SHA}"]),
            SimpleNamespace(id="stale-web", tags=[f"frontiercloud-web:{STALE_SHA}"]),
            SimpleNamespace(id="stale-nginx", tags=[f"frontiercloud-nginx:{STALE_SHA}"]),
            SimpleNamespace(id="unrelated", tags=[f"example:{STALE_SHA}"]),
        ]
        image_api = MagicMock()
        image_api.list.return_value = images
        engine = SimpleNamespace(images=image_api)

        updater.cleanup_release_images(engine, {CURRENT_SHA, PREVIOUS_SHA})

        removed = [item.args[0] for item in image_api.remove.call_args_list]
        self.assertCountEqual(removed, [
            f"frontiercloud-web:{STALE_SHA}",
            f"frontiercloud-nginx:{STALE_SHA}",
        ])
        self.assertNotIn(f"frontiercloud-web:{CURRENT_SHA}", removed)
        self.assertNotIn(f"frontiercloud-nginx:{PREVIOUS_SHA}", removed)
        self.assertNotIn(f"example:{STALE_SHA}", removed)

    def test_cleanup_failure_does_not_delete_retained_generation(self):
        updater = load_updater_module()
        stale = SimpleNamespace(id="stale", tags=[f"frontiercloud-web:{STALE_SHA}"])
        current = SimpleNamespace(id="current", tags=[f"frontiercloud-web:{CURRENT_SHA}"])
        image_api = MagicMock()
        image_api.list.return_value = [stale, current]
        image_api.remove.side_effect = RuntimeError("image busy")
        engine = SimpleNamespace(images=image_api)

        updater.cleanup_release_images(engine, {CURRENT_SHA})

        image_api.remove.assert_called_once_with(
            f"frontiercloud-web:{STALE_SHA}", force=False, noprune=False
        )


class UpdaterRuntimeRestartTests(unittest.TestCase):
    def test_updater_container_is_restart_always_and_is_not_replaced(self):
        compose = (ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
        updater_block = compose.split("  updater:\n", 1)[1].split("\n  web:\n", 1)[0]
        source = (ROOT / "updater/server.py").read_text(encoding="utf-8")
        self.assertIn("restart: always", updater_block)
        self.assertNotIn('"updater"', 'for service in ("secrets-init", "media-init", "web", "nginx")')
        self.assertNotIn("os.exec", source)
        self.assertNotIn("execv", source)
        self.assertIn("os._exit(0)", source)

    def test_restart_request_persists_target_before_process_exit(self):
        updater = load_updater_module()
        updater.RUNTIME_SHA = RUNTIME_SHA
        statuses = []
        maintenance_calls = []

        def record_status(**changes):
            statuses.append(changes)
            return changes

        with (
            patch.object(updater, "write_status", side_effect=record_status),
            patch.object(updater, "maintenance", side_effect=lambda enabled, target="": maintenance_calls.append((enabled, target))),
            patch.object(updater.os, "_exit", side_effect=SystemExit(0)) as exit_process,
        ):
            with self.assertRaises(SystemExit):
                updater.request_runtime_restart(CURRENT_SHA, PREVIOUS_SHA)

        self.assertEqual(maintenance_calls, [])
        self.assertEqual(statuses[-1]["state"], "restarting")
        self.assertEqual(statuses[-1]["phase"], "updater-restart")
        self.assertEqual(statuses[-1]["current_sha"], CURRENT_SHA)
        self.assertEqual(statuses[-1]["previous_sha"], PREVIOUS_SHA)
        self.assertEqual(statuses[-1]["updater_runtime_sha"], RUNTIME_SHA)
        exit_process.assert_called_once_with(0)

    def test_new_runtime_opens_site_only_after_target_sha_is_active(self):
        updater = load_updater_module()
        updater.RUNTIME_SHA = CURRENT_SHA
        maintenance_calls = []
        statuses = []
        with (
            patch.object(updater, "read_status", return_value={
                "state": "restarting", "target_sha": CURRENT_SHA,
                "previous_sha": PREVIOUS_SHA,
            }),
            patch.object(updater, "maintenance", side_effect=lambda enabled, target="": maintenance_calls.append((enabled, target))),
            patch.object(updater, "write_status", side_effect=lambda **changes: statuses.append(changes) or changes),
        ):
            self.assertTrue(updater.complete_pending_restart())

        self.assertEqual(maintenance_calls, [(False, "")])
        self.assertEqual(statuses[-1]["state"], "success")
        self.assertEqual(statuses[-1]["phase"], "complete")
        self.assertEqual(statuses[-1]["current_sha"], CURRENT_SHA)
        self.assertEqual(statuses[-1]["updater_runtime_sha"], CURRENT_SHA)

    def test_runtime_sha_mismatch_keeps_maintenance_closed_and_fails(self):
        updater = load_updater_module()
        updater.RUNTIME_SHA = RUNTIME_SHA
        maintenance_calls = []
        statuses = []
        with (
            patch.object(updater, "read_status", return_value={
                "state": "restarting", "target_sha": CURRENT_SHA,
                "previous_sha": PREVIOUS_SHA,
            }),
            patch.object(updater, "maintenance", side_effect=lambda enabled, target="": maintenance_calls.append((enabled, target))),
            patch.object(updater, "write_status", side_effect=lambda **changes: statuses.append(changes) or changes),
        ):
            self.assertTrue(updater.complete_pending_restart())

        self.assertEqual(maintenance_calls, [(True, CURRENT_SHA)])
        self.assertEqual(statuses[-1]["state"], "failed")
        self.assertEqual(statuses[-1]["phase"], "updater-restart-failed")
        self.assertIn(RUNTIME_SHA, statuses[-1]["detail"])
        self.assertIn(CURRENT_SHA, statuses[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
