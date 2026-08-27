import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from auto_download.media_sync import (
    MediaSynchronizer,
    RemoteItem,
    SyncProfile,
    _expand_remote_info,
    discover_remote_items,
)
from auto_download.profiles import youtube_public_playlists_url


PROFILE = SyncProfile(
    name="test_audio",
    source_url="https://example.invalid/channel",
    media_kind="audio",
    extension="mp3",
    format_selector="bestaudio/best",
    headers={},
)


def remote(media_id: str, title: str) -> RemoteItem:
    return RemoteItem(
        media_id=media_id,
        extractor="Test",
        original_title=title,
        original_playlist="播放列表：一",
        webpage_url=f"https://example.invalid/{media_id}",
    )


class MediaSyncTests(unittest.TestCase):
    def test_multi_source_profile_polls_every_collection_and_keeps_duplicates(self):
        roots = {
            "https://example.invalid/?fid=one": {
                "title": "分类一",
                "entries": [{"id": "same", "title": "歌", "ie_key": "Test"}],
            },
            "https://example.invalid/?fid=two": {
                "title": "分类二",
                "entries": [{"id": "same", "title": "歌", "ie_key": "Test"}],
            },
        }

        class FakeYoutubeDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def extract_info(self, url, download):
                return roots[url]

        fake_module = type("FakeModule", (), {"YoutubeDL": FakeYoutubeDL})
        profile = SyncProfile(
            **{
                **PROFILE.__dict__,
                "source_url": "https://example.invalid/?fid=one",
                "additional_source_urls": ("https://example.invalid/?fid=two",),
            }
        )
        items = discover_remote_items(fake_module, profile)

        self.assertEqual([item.original_playlist for item in items], ["分类一", "分类二"])
        self.assertEqual(len({item.media_id for item in items}), 2)

    def test_youtube_channel_root_is_normalized_to_public_playlists(self):
        self.assertEqual(
            youtube_public_playlists_url("https://www.youtube.com/@wyium"),
            "https://www.youtube.com/@wyium/playlists",
        )
        playlist = "https://www.youtube.com/playlist?list=PL123"
        self.assertEqual(youtube_public_playlists_url(playlist), playlist)

    def test_bilibili_flat_entries_are_hydrated_with_original_titles(self):
        class FakeYdl:
            def extract_info(self, url, download):
                self.requested_url = url
                return {
                    "id": "BV123",
                    "title": "真正的标题",
                    "extractor_key": "BiliBili",
                    "webpage_url": url,
                }

        ydl = FakeYdl()
        root = {
            "title": "收藏夹",
            "entries": [
                {
                    "id": "BV123",
                    "title": "BV123",
                    "ie_key": "BiliBili",
                    "url": "https://www.bilibili.com/video/BV123",
                }
            ],
        }
        items = _expand_remote_info(ydl, root, "https://example.invalid")
        self.assertEqual(items[0].original_title, "真正的标题")
        self.assertEqual(items[0].original_playlist, "收藏夹")

    def test_sync_downloads_clean_names_and_records_original_names(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))

            def downloader(item, target):
                target.write_bytes(item.media_id.encode())
                return True

            report = synchronizer.synchronize(
                [remote("one", "原始：歌名？")],
                downloader,
            )

            target = Path(temporary) / "播放列表_一" / "原始_歌名.mp3"
            self.assertTrue(target.is_file())
            self.assertEqual(report.added, ["播放列表：一 / 原始：歌名？"])
            manifest = json.loads(synchronizer.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["items"]["one"]["original_title"], "原始：歌名？")

    def test_sync_skips_existing_and_deletes_remote_removal(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))

            def downloader(item, target):
                target.write_bytes(b"media")
                return True

            synchronizer.synchronize([remote("one", "保留"), remote("two", "删除")], downloader)
            report = synchronizer.synchronize([remote("one", "保留")], downloader)

            self.assertEqual(report.skipped, ["播放列表：一 / 保留"])
            self.assertEqual(report.deleted, ["播放列表：一 / 删除"])
            self.assertFalse((Path(temporary) / "播放列表_一" / "删除.mp3").exists())

    def test_dry_run_neither_downloads_nor_deletes(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))
            stale = Path(temporary) / "播放列表_一" / "旧歌.mp3"
            stale.parent.mkdir()
            stale.write_bytes(b"media")
            synchronizer.save_manifest(
                synchronizer.plan_items([remote("old", "旧歌")])
            )

            def forbidden_downloader(item, target):
                raise AssertionError("dry-run invoked downloader")

            report = synchronizer.synchronize(
                [remote("new", "新歌")],
                forbidden_downloader,
                dry_run=True,
            )
            self.assertTrue(stale.exists())
            self.assertEqual(report.deleted, ["播放列表：一 / 旧歌"])
            self.assertEqual(report.added, ["播放列表：一 / 新歌"])

    def test_dry_run_does_not_report_adopted_legacy_name_as_deleted(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))
            legacy = Path(temporary) / "播放列表_一" / "旧：标题.mp3"
            legacy.parent.mkdir()
            legacy.write_bytes(b"media")

            report = synchronizer.synchronize(
                [remote("one", "旧：标题")],
                lambda item, target: False,
                dry_run=True,
            )

            self.assertEqual(report.skipped, ["播放列表：一 / 旧：标题"])
            self.assertEqual(report.deleted, [])
            self.assertTrue(legacy.exists())

    def test_sync_deletes_untracked_media_from_managed_playlist(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))
            extra = Path(temporary) / "播放列表_一" / "远端不存在.mp3"
            extra.parent.mkdir()
            extra.write_bytes(b"extra")

            def downloader(item, target):
                target.write_bytes(b"media")
                return True

            report = synchronizer.synchronize([remote("one", "保留")], downloader)
            self.assertFalse(extra.exists())
            self.assertIn("播放列表_一/远端不存在.mp3", report.deleted)

    def test_sync_cleans_extras_when_entire_playlist_disappears(self):
        with TemporaryDirectory() as temporary:
            synchronizer = MediaSynchronizer(PROFILE, Path(temporary))

            def downloader(item, target):
                target.write_bytes(b"media")
                return True

            synchronizer.synchronize([remote("one", "曾经存在")], downloader)
            extra = Path(temporary) / "播放列表_一" / "额外文件.mp3"
            extra.write_bytes(b"extra")
            report = synchronizer.synchronize([], downloader)

            self.assertFalse(extra.exists())
            self.assertIn("播放列表_一/额外文件.mp3", report.deleted)

    def test_untracked_pruning_waits_for_peer_manifest(self):
        with TemporaryDirectory() as temporary:
            profile = SyncProfile(
                **{
                    **PROFILE.__dict__,
                    "name": "source_one",
                    "peer_profiles": ("source_two",),
                }
            )
            synchronizer = MediaSynchronizer(profile, Path(temporary))
            extra = Path(temporary) / "播放列表_一" / "其他来源.mp3"
            extra.parent.mkdir()
            extra.write_bytes(b"peer")

            report = synchronizer.synchronize([], lambda item, target: False)

            self.assertTrue(extra.exists())
            self.assertEqual(report.deleted, [])


if __name__ == "__main__":
    unittest.main()
