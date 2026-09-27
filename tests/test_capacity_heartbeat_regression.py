"""Regressions for storage capacity observability and heartbeat health isolation."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class StorageCapacityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_exposes_live_physical_and_project_capacity_facts(self):
        from app.services import resource_pool, storage_capacity

        summary = {
            "allocated_bytes": 900,
            "used_bytes": 300,
            "available_bytes": 1,
            "online_writable_bytes": 1,
            "members": [
                {
                    "member_id": "master",
                    "member_kind": "MasterLocal",
                    "storage_enabled": 1,
                    "allocated_bytes": 300,
                    "used_bytes": 100,
                    "reserved_bytes": 0,
                    "physical_free_bytes": 1,
                    "health": "online",
                    "writable": 1,
                    "available_bytes": 1,
                },
                {
                    "member_id": "follower",
                    "member_kind": "Follower",
                    "storage_enabled": 1,
                    "allocated_bytes": 600,
                    "used_bytes": 200,
                    "reserved_bytes": 0,
                    "physical_free_bytes": 2,
                    "health": "online",
                    "writable": 1,
                    "available_bytes": 2,
                },
                {
                    "member_id": "auto",
                    "member_kind": "Auto",
                    "storage_enabled": 1,
                    "allocated_bytes": 0,
                    "used_bytes": 0,
                    "reserved_bytes": 0,
                    "physical_free_bytes": 0,
                    "health": "online",
                    "writable": 1,
                    "available_bytes": 2,
                },
            ],
        }
        store = SimpleNamespace(list_relationships=AsyncMock(return_value=[{
            "state": "active",
            "peer_id": "follower",
            "summary": {"storage": {
                "physical_total_bytes": 1000,
                "physical_free_bytes": 700,
            }},
        }]))

        with (
            patch.object(storage_capacity, "local_physical_capacity", return_value=(5000, 2000)),
            patch.object(resource_pool, "PHYSICAL_RESERVE_BYTES", 100),
        ):
            result = await storage_capacity.enrich_pool_summary(summary, store)

        members = {row["member_id"]: row for row in result["members"]}
        self.assertEqual(members["master"]["physical_total_bytes"], 5000)
        self.assertEqual(members["master"]["physical_free_bytes"], 2000)
        self.assertEqual(members["master"]["current_allocated_bytes"], 300)
        self.assertEqual(members["master"]["project_used_bytes"], 100)
        self.assertEqual(members["master"]["available_bytes"], 200)
        self.assertEqual(members["follower"]["physical_total_bytes"], 1000)
        self.assertEqual(members["follower"]["physical_free_bytes"], 700)
        self.assertEqual(members["follower"]["available_bytes"], 400)
        self.assertEqual(members["auto"]["available_bytes"], 400)
        self.assertEqual(result["physical_total_bytes"], 6000)
        self.assertEqual(result["physical_free_bytes"], 2700)
        self.assertEqual(result["current_allocated_bytes"], 900)
        self.assertEqual(result["project_used_bytes"], 300)
        self.assertEqual(result["available_bytes"], 600)
        self.assertEqual(result["online_writable_bytes"], 600)


class HeartbeatIsolationRegressionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def relation(direction: str) -> dict:
        return {
            "relationship_id": "a" * 32,
            "peer_id": "b" * 32,
            "peer_endpoint": "https://peer.example",
            "peer_key": "c" * 64,
            "credential": "credential",
            "direction": direction,
            "mode": "Relay",
            "state": "active",
            "status": "online",
            "summary": {},
            "created_at": 0,
        }

    @staticmethod
    def fake_state() -> SimpleNamespace:
        return SimpleNamespace(
            database=object(),
            unseal=lambda value: value,
            heartbeat=AsyncMock(),
        )

    async def test_backup_failure_cannot_turn_successful_heartbeat_into_failure(self):
        from app.services.federation import protocol as p
        from app.services.federation import runtime as runtime_module

        fake_state = self.fake_state()
        controller = runtime_module.Runtime()
        controller.call = AsyncMock(return_value={"protocol": p.PROTOCOL_VERSION})
        controller.maybe_backup = AsyncMock(side_effect=RuntimeError("backup failed"))
        relation = self.relation("downstream")

        with (
            patch.object(runtime_module, "state", fake_state),
            patch("app.services.resource_pool.member_configuration", AsyncMock(return_value={})),
        ):
            await controller.tick(relation)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        self.assertEqual(fake_state.heartbeat.await_count, 1)
        heartbeat_args = fake_state.heartbeat.await_args.args
        self.assertEqual(heartbeat_args[0], relation["relationship_id"])
        self.assertIs(heartbeat_args[1], True)

    async def test_upstream_tick_is_heartbeat_only_after_worker_retirement(self):
        from app.services.federation import protocol as p
        from app.services.federation import runtime as runtime_module

        fake_state = self.fake_state()
        controller = runtime_module.Runtime()
        controller.call = AsyncMock(return_value={"protocol": p.PROTOCOL_VERSION})
        relation = self.relation("upstream")

        with patch.object(runtime_module, "state", fake_state):
            await controller.tick(relation)

        self.assertEqual(fake_state.heartbeat.await_count, 1)
        self.assertEqual(controller.call.await_count, 1)
        self.assertEqual(controller.call.await_args.args[1], "/internal/v1/heartbeat")
        self.assertFalse(hasattr(controller, "fill_worker_slots"))

    async def test_full_backup_builds_are_serialized_across_followers(self):
        from app.services.federation import runtime as runtime_module

        controller = runtime_module.Runtime()
        active = 0
        maximum = 0

        async def backup(_relation):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1

        controller.maybe_backup = AsyncMock(side_effect=backup)
        first = self.relation("downstream")
        second = {**self.relation("downstream"), "relationship_id": "d" * 32,
                  "peer_id": "e" * 32}
        controller.schedule_backup(first)
        controller.schedule_backup(second)
        tasks = list(controller.backup_tasks.values())
        await asyncio.gather(*tasks)

        self.assertEqual(controller.maybe_backup.await_count, 2)
        self.assertEqual(maximum, 1)


if __name__ == "__main__":
    unittest.main()
