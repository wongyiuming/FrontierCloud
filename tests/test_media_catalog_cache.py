import unittest
from unittest.mock import AsyncMock, patch

from app.api.v1 import media
from app.services import media_catalog_cache


class _FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value


class MediaCatalogCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_invalidation_makes_previous_catalog_unreachable(self):
        fake_redis = _FakeRedis()
        catalog = [{"title": "cached-track"}]

        with (
            patch.object(media_catalog_cache, "redis_client", fake_redis),
            patch.object(media_catalog_cache.settings, "MEDIA_CATALOG_CACHE_TTL", 300),
        ):
            generation, cached = await media_catalog_cache.load_media_catalog("tracks", "audio:test")
            self.assertEqual(generation, 0)
            self.assertIsNone(cached)

            await media_catalog_cache.store_media_catalog(generation, "tracks", "audio:test", catalog)
            generation, cached = await media_catalog_cache.load_media_catalog("tracks", "audio:test")
            self.assertEqual(cached, catalog)

            await media_catalog_cache.invalidate_media_catalog()
            new_generation, cached = await media_catalog_cache.load_media_catalog("tracks", "audio:test")

        self.assertEqual(new_generation, 1)
        self.assertIsNone(cached)
        self.assertIn(300, fake_redis.ttls.values())

    async def test_track_cache_hit_skips_mysql_and_filesystem_scan(self):
        expected = [{"title": "from-redis"}]
        with (
            patch.object(media, "load_media_catalog", new=AsyncMock(return_value=(3, expected))),
            patch.object(media, "_hidden_set", new=AsyncMock()) as hidden_set,
            patch.object(media.asyncio, "to_thread", new=AsyncMock()) as to_thread,
        ):
            result = await media.scan_media_files_by_category("test", media.AUDIO_EXTS, "audio")

        self.assertEqual(result, expected)
        hidden_set.assert_not_awaited()
        to_thread.assert_not_awaited()

    async def test_category_cache_hit_skips_mysql_and_recursive_scan(self):
        expected = [{"name": "cached-category"}]
        with (
            patch.object(media, "load_media_catalog", new=AsyncMock(return_value=(3, expected))),
            patch.object(media, "_hidden_set", new=AsyncMock()) as hidden_set,
            patch.object(media.asyncio, "to_thread", new=AsyncMock()) as to_thread,
        ):
            result = await media.get_media_categories("music", media.AUDIO_EXTS)

        self.assertEqual(result, expected)
        hidden_set.assert_not_awaited()
        to_thread.assert_not_awaited()

    async def test_track_cache_miss_scans_and_populates_redis(self):
        expected = [{"title": "from-filesystem"}]
        store = AsyncMock()
        with (
            patch.object(media, "load_media_catalog", new=AsyncMock(return_value=(4, None))),
            patch.object(media, "_hidden_set", new=AsyncMock(return_value=set())),
            patch.object(media.asyncio, "to_thread", new=AsyncMock(return_value=expected)) as to_thread,
            patch.object(media, "store_media_catalog", new=store),
        ):
            result = await media.scan_media_files_by_category("test", media.AUDIO_EXTS, "audio")

        self.assertEqual(result, expected)
        to_thread.assert_awaited_once()
        store.assert_awaited_once_with(4, "tracks", "audio:test", expected)


if __name__ == "__main__":
    unittest.main()
