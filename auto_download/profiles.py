"""Remote source profiles shared by Windows and CentOS entry points."""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit, urlunsplit

from .media_sync import SyncProfile


def youtube_public_playlists_url(source_url: str) -> str:
    """Convert a YouTube channel root URL to its public playlists tab."""
    parsed = urlsplit(source_url.strip())
    path = parsed.path.rstrip("/")
    is_channel_root = path.startswith("/@") or path.startswith("/channel/")
    if is_channel_root and path.count("/") <= 2:
        path = f"{path}/playlists"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))


YOUTUBE_URL = youtube_public_playlists_url("https://www.youtube.com/@wyium")
BILIBILI_URLS = (
    "https://space.bilibili.com/50687441/favlist?fid=4032917841&ftype=create",
    "https://space.bilibili.com/50687441/favlist?fid=4041747841&ftype=create",
    "https://space.bilibili.com/50687441/favlist?fid=4077706241&ftype=create",
    "https://space.bilibili.com/50687441/favlist?fid=4086690941&ftype=create",
    "https://space.bilibili.com/50687441/favlist?fid=4113601541&ftype=create",
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
    source_url=BILIBILI_URLS[0],
    media_kind="audio",
    extension="mp3",
    format_selector="bestaudio/best",
    headers=BILIBILI_HEADERS,
    retries=10,
    peer_profiles=("youtube_audio",),
    additional_source_urls=BILIBILI_URLS[1:],
)
BILIBILI_MP4 = SyncProfile(
    name="bilibili_video",
    source_url=BILIBILI_URLS[0],
    media_kind="video",
    extension="mp4",
    format_selector="bestvideo+bestaudio/best",
    headers=BILIBILI_HEADERS,
    retries=10,
    peer_profiles=("youtube_video",),
    additional_source_urls=BILIBILI_URLS[1:],
)


def with_source_url(profile: SyncProfile, source_url: str) -> SyncProfile:
    """Return a profile using a command-line URL override."""
    if profile.name.startswith("youtube_"):
        source_url = youtube_public_playlists_url(source_url)
    return replace(profile, source_url=source_url, additional_source_urls=())
