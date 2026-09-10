import unittest
from unittest.mock import patch

from app.services import media_objects


class _RegistryConnection:
    def __init__(self, legacy_exists=False):
        self.legacy_exists = legacy_exists
        self.by_path = {}
        self.executed = []

    async def scalar(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "FROM media_objects" in sql:
            return self.by_path.get(params["media_path"])
        if "EXISTS" in sql:
            return 1 if self.legacy_exists else 0
        return None

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        self.executed.append((sql, params))
        if "INSERT IGNORE INTO media_objects" in sql:
            self.by_path.setdefault(params["media_path"], params["media_id"])


class MediaObjectIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_object_gets_random_identity_that_is_reused(self):
        connection = _RegistryConnection()
        with patch.object(media_objects.secrets, "token_hex", return_value="a" * 64):
            first = await media_objects.ensure_object(
                connection, "music/artist/song.mp3", "audio"
            )
            second = await media_objects.ensure_object(
                connection, "music/artist/song.mp3", "audio"
            )

        self.assertEqual(first, "a" * 64)
        self.assertEqual(second, first)
        inserts = [sql for sql, _params in connection.executed if "INSERT IGNORE" in sql]
        self.assertEqual(len(inserts), 1)

    async def test_existing_path_identity_is_adopted_for_lossless_migration(self):
        connection = _RegistryConnection(legacy_exists=True)
        path = "music/artist/existing.mp3"
        resolved = await media_objects.ensure_object(connection, path, "audio")
        self.assertEqual(resolved, media_objects.legacy_object_id(path))

    async def test_object_kind_is_restricted(self):
        with self.assertRaises(ValueError):
            await media_objects.ensure_object(_RegistryConnection(), "music/a.mp3", "other")


if __name__ == "__main__":
    unittest.main()
