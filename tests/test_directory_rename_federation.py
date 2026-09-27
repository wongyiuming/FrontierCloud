from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch, call

from app.services import media_directories


class _Begin:
    def __init__(self):
        self.conn = object()

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, _kind, _value, _traceback):
        return False


class _Database:
    def __init__(self):
        self.contexts: list[_Begin] = []

    def begin(self):
        context = _Begin()
        self.contexts.append(context)
        return context


class MasterDirectoryRenameRuntimeTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def rows():
        return [
            {
                "owner_id": "master-id",
                "relationship_id": None,
                "resource_id": "a" * 64,
                "path": "music/shared/local.wav",
                "health": "online",
            },
            {
                "owner_id": "follower-id",
                "relationship_id": "relation-id",
                "resource_id": "b" * 64,
                "path": "music/shared/remote.wav",
                "health": "online",
            },
        ]

    async def test_master_renames_every_storage_owner_before_committing_catalog(self):
        database = _Database()
        fake_state = SimpleNamespace(
            node={"role": "Master", "node_id": "master-id"},
            database=database,
        )
        physical = []
        remote = AsyncMock()
        rewrite_master = AsyncMock()
        rewrite_local = AsyncMock()
        with (
            patch.object(media_directories, "node_state", fake_state),
            patch.object(
                media_directories, "_preflight_master", new=AsyncMock(return_value=self.rows()),
            ),
            patch.object(
                media_directories,
                "_rename_physical_directory",
                side_effect=lambda old, new: physical.append((old, new)),
            ),
            patch.object(media_directories, "_remote_rename", new=remote),
            patch.object(media_directories, "_rewrite_master_catalog", new=rewrite_master),
            patch.object(media_directories, "_rewrite_local_metadata", new=rewrite_local),
            patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()),
        ):
            result = await media_directories.rename_directory("music/shared", "renamed")

        self.assertEqual(result["new_path"], "music/renamed")
        self.assertEqual(result["storage_members"], 2)
        self.assertEqual(result["media_count"], 2)
        self.assertEqual(physical, [("music/shared", "music/renamed")])
        remote.assert_awaited_once_with(self.rows()[1], "music/shared", "music/renamed")
        self.assertEqual(len(database.contexts), 1)
        rewrite_master.assert_awaited_once_with(
            database.contexts[0].conn, "music/shared", "music/renamed",
        )
        rewrite_local.assert_awaited_once_with(
            database.contexts[0].conn, "music/shared", "music/renamed",
        )

    async def test_remote_failure_rolls_back_already_moved_master_local_directory(self):
        database = _Database()
        fake_state = SimpleNamespace(
            node={"role": "Master", "node_id": "master-id"},
            database=database,
        )
        physical = []
        remote = AsyncMock(side_effect=RuntimeError("follower unavailable"))
        with (
            patch.object(media_directories, "node_state", fake_state),
            patch.object(
                media_directories, "_preflight_master", new=AsyncMock(return_value=self.rows()),
            ),
            patch.object(
                media_directories,
                "_rename_physical_directory",
                side_effect=lambda old, new: physical.append((old, new)),
            ),
            patch.object(media_directories, "_remote_rename", new=remote),
            patch.object(media_directories, "_rewrite_master_catalog", new=AsyncMock()),
            patch.object(media_directories, "_rewrite_local_metadata", new=AsyncMock()),
            patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "follower unavailable"):
                await media_directories.rename_directory("music/shared", "renamed")

        self.assertEqual(physical, [
            ("music/shared", "music/renamed"),
            ("music/renamed", "music/shared"),
        ])
        self.assertEqual(len(database.contexts), 0)

    async def test_master_metadata_failure_rolls_back_remote_then_local_in_reverse_order(self):
        database = _Database()
        fake_state = SimpleNamespace(
            node={"role": "Master", "node_id": "master-id"},
            database=database,
        )
        physical = []
        remote = AsyncMock()
        with (
            patch.object(media_directories, "node_state", fake_state),
            patch.object(
                media_directories, "_preflight_master", new=AsyncMock(return_value=self.rows()),
            ),
            patch.object(
                media_directories,
                "_rename_physical_directory",
                side_effect=lambda old, new: physical.append((old, new)),
            ),
            patch.object(media_directories, "_remote_rename", new=remote),
            patch.object(
                media_directories,
                "_rewrite_master_catalog",
                new=AsyncMock(side_effect=RuntimeError("catalog commit failed")),
            ),
            patch.object(media_directories, "_rewrite_local_metadata", new=AsyncMock()),
            patch.object(media_directories, "invalidate_media_catalog", new=AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "catalog commit failed"):
                await media_directories.rename_directory("music/shared", "renamed")

        self.assertEqual(physical, [
            ("music/shared", "music/renamed"),
            ("music/renamed", "music/shared"),
        ])
        self.assertEqual(remote.await_args_list, [
            call(self.rows()[1], "music/shared", "music/renamed"),
            call(self.rows()[1], "music/renamed", "music/shared"),
        ])


if __name__ == "__main__":
    unittest.main()
