"""Remote source profiles shared by Windows and CentOS entry points."""

from __future__ import annotations

from dataclasses import replace

from .media_sync import SyncProfile


YOUTUBE_URL = "https://www.youtube.com/@%E7%BE%BD%E6%B1%9F-f4k/playlists"
BILIBILI_AUDIO_URL = (
    "https://space.bilibili.com/50687441/favlist?fid=4086690941&ftype=create"
)
BILIBILI_VIDEO_URL = (
    "https://space.bilibili.com/50687441/favlist?fid=4077706241&ftype=create"
)

YOUTUBE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
BILIBILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://www.bilibili.com/",
}

YOUTUBE_MP3 = SyncProfile(
    name="youtube_audio",
    source_url=YOUTUBE_URL,
    media_kind="audio",
    extension="mp3",
    format_selector="bestaudio/best",
    headers=YOUTUBE_HEADERS,
    peer_profiles=("bilibili_audio",),
)
YOUTUBE_MP4 = SyncProfile(
    name="youtube_video",
    source_url=YOUTUBE_URL,
    media_kind="video",
    extension="mp4",
    format_selector="bestvideo+bestaudio/best",
    headers=YOUTUBE_HEADERS,
    peer_profiles=("bilibili_video",),
)
BILIBILI_MP3 = SyncProfile(
    name="bilibili_audio",
    source_url=BILIBILI_AUDIO_URL,
    media_kind="audio",
    extension="mp3",
    format_selector="bestaudio/best",
    headers=BILIBILI_HEADERS,
    retries=10,
    peer_profiles=("youtube_audio",),
)
BILIBILI_MP4 = SyncProfile(
    name="bilibili_video",
    source_url=BILIBILI_VIDEO_URL,
    media_kind="video",
    extension="mp4",
    format_selector="bestvideo+bestaudio/best",
    headers=BILIBILI_HEADERS,
    retries=10,
    peer_profiles=("youtube_video",),
)


def with_source_url(profile: SyncProfile, source_url: str) -> SyncProfile:
    """Return a profile using a command-line URL override."""
    return replace(profile, source_url=source_url)
